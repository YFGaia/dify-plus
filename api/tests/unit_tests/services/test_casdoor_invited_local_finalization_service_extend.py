"""Actual D/E/F producer and adversarial SQL rollback; offline SQLite only."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from enum import StrEnum
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.claims import StructuredUserRef
from core.casdoor.invited_finalization_receipt import (
    FULL_HISTORY_FIELDS,
    MODE,
    PLAN_DOMAIN,
    RECEIPT_KIND,
    ROWS_DOMAIN,
    InvitedLocalFinalizationReceipt,
    canonical,
    finalized_rows_sha256,
    mapping_plan_sha256,
)
from core.casdoor.invited_write_receipt import RECEIPT_KIND as D_ACTION
from core.casdoor.leases import CasdoorLeaseError, CasdoorLeases
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invited_local_finalization_repository_extend import (
    CasdoorInvitedLocalFinalizationRepository,
)
from services.casdoor_invited_local_finalization_service_extend import (
    CasdoorInvitedLocalFinalizationService,
)
from services.casdoor_invited_write_receipt_service_extend import (
    CasdoorInvitedWriteReceiptService,
)

from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    database,
    persist,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def service(case, factory=None):
    return CasdoorInvitedLocalFinalizationService(
        session_factory=factory or case.factory, configuration_factory=configuration_factory
    )


def finalize(case, **kwargs):
    return service(case, kwargs.pop("factory", None)).finalize_invited_local_memberships(
        case.attempt,
        roles=kwargs.get("roles", case.roles),
        leases=kwargs.get("leases", case.leases),
        deadline=kwargs.get("deadline", case.deadline),
    )


def f_rows(case):
    with case.db() as session:
        return tuple(session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == RECEIPT_KIND)))


def all_state(case):
    return dict(database(case), audits=audits(case))


def audits(case):
    with case.db() as session:
        return tuple(session.execute(sa.select(*Audit.__table__.columns).order_by(Audit.id)))


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("invite_target", [False, True])
@pytest.mark.parametrize("preserve", ["none", "unmanaged", "owner", "all"])
def test_real_producer_exact_canonical_receipt_and_unchanged_other_rows(writer_case, absent, invite_target, preserve):
    case = writer_case(absent=absent, invite_target=invite_target)
    if preserve != "none":
        with case.db() as session, session.begin():
            workspace_ids = [str(case.configuration.workspace_mappings[0].workspace_id)]
            if preserve == "all":
                workspace_ids.append(str(case.configuration.default_workspace_id))
            for wid in set(workspace_ids):
                if not session.scalar(
                    sa.select(Join.id).where(Join.account_id == case.ids["account"], Join.tenant_id == wid)
                ):
                    session.add(
                        Join(
                            account_id=case.ids["account"],
                            tenant_id=wid,
                            role="owner" if preserve == "owner" else "normal",
                            current=False,
                        )
                    )
    d_result = persist(case)
    before = all_state(case)
    d_audit = next(r for r in audits(case) if r.action == D_ACTION)
    receipt = finalize(case)
    assert type(receipt) is InvitedLocalFinalizationReceipt
    data = receipt.values()
    assert len(f_rows(case)) == 1
    audit = f_rows(case)[0]
    assert audit.summary_json == receipt.canonical_json == canonical(data)
    assert audit.result_code == "verified" and audit.actor_account_id is None
    assert data["mode"] == MODE and data["generation"] == 1
    assert data["write_receipt_sha256"] == hashlib.sha256(d_audit.summary_json.encode()).hexdigest()
    assert data["before_postwrite_sha256"] == json.loads(d_audit.summary_json)["postwrite_sha256"]
    ids = sorted(str(r.membership_id) for r in d_result.workspaces if r.membership_created)
    assert data["finalized_ids"] == ids
    assert len(data["finalized_ids"]) == len(set(data["finalized_ids"]))
    assert data["references"] == json.loads(d_audit.summary_json)["references"]
    after = all_state(case)
    for key in before:
        if key not in ("casdoor_managed_membership_extend", "audits"):
            assert after[key] == before[key]
    histories = after["casdoor_managed_membership_extend"]
    assert all(r.finalization == "finalized" for r in histories)
    for old, current in zip(before["casdoor_managed_membership_extend"], histories, strict=True):
        assert dict(current._mapping) == dict(
            old._mapping, finalization=current.finalization, updated_at=current.updated_at
        )
    rows = tuple({k: v.value if isinstance(v, StrEnum) else v for k, v in r._mapping.items()} for r in histories)
    assert data["finalized_rows_sha256"] == finalized_rows_sha256(audit.correlation_id, rows)
    assert set(History.__table__.columns.keys()) == set(FULL_HISTORY_FIELDS)
    for text in ("ExactSubject", "invitee@example.test", "role_graph", "payload_json", "password", "token"):
        assert text not in receipt.canonical_json
    with pytest.raises(FrozenInstanceError):
        receipt.canonical_json = "changed"
    data["references"]["account_id"] = str(uuid4())
    assert receipt.values()["references"]["account_id"] == case.ids["account"]
    if ids:
        with pytest.raises(ValueError):
            CasdoorInvitedWriteReceiptService(
                session_factory=case.factory, configuration_factory=configuration_factory
            ).observe_invited_write_receipt(case.attempt)
    with pytest.raises(ValueError):
        finalize(case)
    assert all_state(case) == after


@pytest.mark.parametrize("kind", ["none", "object", "dictionary", "subject", "org", "empty", "duplicate"])
def test_wrong_current_snapshot_never_finalizes_or_appends(writer_case, kind):
    case = writer_case()
    persist(case)
    before = all_state(case)
    roles = {
        "none": None,
        "object": object(),
        "dictionary": {},
        "subject": replace(case.roles, subject="different"),
        "org": replace(case.roles, user_ref=StructuredUserRef("OtherOrg", "person")),
        "empty": replace(case.roles, effective_roles=()),
        "duplicate": replace(case.roles, effective_roles=case.roles.effective_roles * 2),
    }[kind]
    with pytest.raises(ValueError):
        finalize(case, roles=roles)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize(
    "kind",
    [
        "missing-history",
        "extra-history",
        "wrong-ownership",
        "source",
        "nonpending",
        "desired-role",
        "baseline",
        "fingerprint",
        "oversize",
        "identity",
        "join-role",
        "missing-join",
        "workspace",
        "account",
        "intent",
        "other-intent",
        "d-digest",
        "d-generation",
        "d-fence",
        "d-namespace",
        "d-duplicate",
        "d-missing",
        "uninitialized",
        "lifecycle",
        "issuance",
        "config",
        "fence",
        "namespace",
    ],
)
def test_current_complete_scope_or_d_drift_fails_closed(writer_case, kind):
    from models.casdoor_extend import CasdoorIntegrationExtend as Integration
    from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
    from models.invitation_authority_extend import (
        InvitationAuthorityIssuanceExtend as Issuance,
    )
    from models.invitation_authority_extend import (
        InvitationAuthorityLifecycleExtend as Lifecycle,
    )

    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        if kind in ("missing-history", "missing-join", "d-missing"):
            model = {"missing-history": History, "missing-join": Join, "d-missing": Audit}[kind]
            query = sa.delete(model)
            if model is Join:
                query = query.where(Join.tenant_id == str(case.configuration.default_workspace_id))
            session.execute(query)
        elif kind in ("extra-history", "other-intent", "d-duplicate"):
            model = {"extra-history": History, "other-intent": Intent, "d-duplicate": Audit}[kind]
            query = sa.select(*model.__table__.columns)
            if model is Audit:
                query = query.where(Audit.action == D_ACTION)
            row = dict(session.execute(query.limit(1)).one()._mapping)
            row["id"] = str(uuid4())
            if model is History:
                row["workspace_id"] = str(uuid4())
            if model is Intent:
                row["idempotency_key"] = uuid4().hex * 2
            session.execute(sa.insert(model).values(**row))
        elif kind.startswith("d-"):
            row = session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == D_ACTION)).one()
            data = json.loads(row.summary_json)
            if kind == "d-digest":
                data["postwrite_sha256"] = "0" * 64
            elif kind == "d-generation":
                data["generation_before"], data["generation_after"] = 1, 2
            elif kind == "d-fence":
                data["fence_epoch"] += 1
            elif kind == "d-namespace":
                data["references"]["namespace_id"] = str(uuid4())
            session.execute(sa.update(Audit).where(Audit.id == row.id).values(summary_json=canonical(data)))
        else:
            model, values = {
                "wrong-ownership": (History, {"ownership": "local_override"}),
                "source": (History, {"source": "mapping"}),
                "nonpending": (History, {"finalization": "finalized"}),
                "desired-role": (History, {"desired_roles_json": "{}"}),
                "baseline": (History, {"baseline_json": "{}"}),
                "fingerprint": (History, {"last_applied_fingerprint": "0" * 64}),
                "oversize": (History, {"desired_roles_json": "é" * 17000}),
                "identity": (Identity, {"sync_generation": 2}),
                "join-role": (Join, {"role": "owner"}),
                "workspace": (Tenant, {"status": "archive"}),
                "account": (Account, {"status": "banned"}),
                "uninitialized": (Account, {"initialized_at": None}),
                "intent": (Intent, {"error_code": "changed"}),
                "lifecycle": (Lifecycle, {"state": "withdrawn"}),
                "issuance": (Issuance, {"payload_json": "{}"}),
                "config": (Integration, {"enabled": False}),
                "fence": (Namespace, {"fence_epoch": 99}),
                "namespace": (Namespace, {"lifecycle": "archived"}),
            }[kind]
            query = sa.update(model)
            if kind == "source":
                query = query.where(History.source == "fallback")
            session.execute(query.values(**values))
    before = all_state(case)
    with pytest.raises(ValueError):
        finalize(case)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize("field", FULL_HISTORY_FIELDS)
def test_cas_conflict_on_every_exact_history_column_rolls_back_root(writer_case, monkeypatch, field):
    case = writer_case()
    persist(case)
    before = all_state(case)
    original = CasdoorInvitedLocalFinalizationRepository._cas
    calls = []

    def conflict(owner, row):
        from datetime import timedelta

        calls.append(field)
        old = getattr(row, field)
        if field == "id":
            value = str(uuid4())
        elif field.endswith("_id"):
            value = str(uuid4())
        elif field in ("created_at", "updated_at"):
            value = old + timedelta(microseconds=1)
        elif field in ("ownership_epoch", "desired_generation"):
            value = old + 1
        elif field == "ownership":
            value = "local_override"
        elif field == "source":
            value = "fallback" if old == "mapping" else "mapping"
        elif field == "finalization":
            value = "finalized"
        elif field == "tombstone":
            value = True
        elif field == "last_applied_fingerprint":
            value = "0" * 64
        else:
            value = "{}"
        owner.session.execute(sa.update(History).where(History.id == row.id).values({field: value}))
        return original(owner, row)

    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "_cas", conflict)
    with pytest.raises(ValueError):
        finalize(case)
    assert calls == [field] and all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize("which", [1, 2])
@pytest.mark.parametrize("after", [False, True])
def test_each_cas_failure_rolls_back_all_rows(writer_case, monkeypatch, which, after):
    case = writer_case()
    persist(case)
    before = all_state(case)
    original = CasdoorInvitedLocalFinalizationRepository._cas
    calls = []

    def failure(owner, row):
        calls.append(row.id)
        if len(calls) == which:
            if after:
                original(owner, row)
            raise RuntimeError("CAS injected")
        return original(owner, row)

    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "_cas", failure)
    with pytest.raises(RuntimeError, match="CAS injected"):
        finalize(case)
    assert len(calls) == which and all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize("kind", ["flush", "audit-before", "audit-after", "readback", "initial-read", "final-read"])
def test_business_flush_append_and_each_full_readback_fault_roll_back(writer_case, monkeypatch, kind):
    case = writer_case()
    persist(case)
    before = all_state(case)
    if kind == "flush":
        original = sa.orm.Session.flush

        def fail(session, *args, **kwargs):
            with session.no_autoflush:
                changed = session.scalar(
                    sa.select(sa.func.count()).select_from(History).where(History.finalization == "finalized")
                )
            if changed:
                raise RuntimeError("injected")
            return original(session, *args, **kwargs)

        # no_autoflush/SQL Core keeps the injector from recursively calling flush.
        def factory():
            session = case.factory()
            session.autoflush = False
            return session

        monkeypatch.setattr(sa.orm.Session, "flush", fail)
    else:
        factory = None
        if kind in ("audit-before", "audit-after"):
            original = CasdoorAuditRepository._append_invited_finalization_receipt

            def fail(owner, *args, **kwargs):
                if kind == "audit-after":
                    original(owner, *args, **kwargs)
                raise RuntimeError("injected")

            monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", fail)
        elif kind == "readback":

            def fail(*_args, **_kwargs):
                raise RuntimeError("injected")

            monkeypatch.setattr(CasdoorAuditRepository, "_read_invited_finalization_receipt", fail)
        else:
            original = CasdoorInvitedLocalFinalizationRepository._reread
            calls = []

            def fail(owner):
                calls.append(True)
                original(owner)
                if len(calls) == (1 if kind == "initial-read" else 2):
                    raise RuntimeError("injected")

            monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "_reread", fail)
    with pytest.raises(RuntimeError, match="injected"):
        finalize(case, factory=factory)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize(
    "kind", ["lost-before", "lost-last", "wrong-deadline", "expired", "wrong-scope", "rbac", "edition"]
)
def test_lease_budget_policy_barriers_leave_no_effect(writer_case, monkeypatch, kind):
    from configs import dify_config
    from core.casdoor.leases import CasdoorLeaseScope

    case = writer_case()
    persist(case)
    before = all_state(case)
    calls = []
    original = CasdoorLeases.ensure_owned
    if kind in ("lost-before", "lost-last"):

        def lost(owner, *args, **kwargs):
            calls.append(True)
            if len(calls) == (1 if kind == "lost-before" else 2):
                raise CasdoorLeaseError("lost")
            return original(owner, *args, **kwargs)

        monkeypatch.setattr(CasdoorLeases, "ensure_owned", lost)
    elif kind == "wrong-scope":
        case.leases._keys = CasdoorLeaseScope(UUID(int=2), "different").canonical_keys
    elif kind == "rbac":
        monkeypatch.setattr(dify_config, "RBAC_ENABLED", True)
    elif kind == "edition":
        monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", "ENTERPRISE")
    deadline = 0 if kind == "expired" else case.deadline + 1 if kind == "wrong-deadline" else case.deadline
    with pytest.raises((ValueError, CasdoorLeaseError)):
        finalize(case, deadline=deadline)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize("kind", ["history", "join", "identity", "intent", "audit", "d-audit", "account", "workspace"])
def test_last_lease_io_drift_detected_by_following_complete_sql_reread(writer_case, monkeypatch, kind):
    case = writer_case()
    persist(case)
    before = all_state(case)
    original = CasdoorInvitedLocalFinalizationRepository._leases
    calls = []

    def changed(owner):
        original(owner)
        calls.append(True)
        if len(calls) == 2:
            model, values = {
                "history": (History, {"baseline_json": "{}"}),
                "join": (Join, {"current": True}),
                "identity": (Identity, {"sync_generation": 2}),
                "intent": (Intent, {"error_code": "changed"}),
                "audit": (Audit, {"result_code": "changed"}),
                "d-audit": (Audit, {"result_code": "changed"}),
                "account": (Account, {"name": "changed"}),
                "workspace": (Tenant, {"name": "changed"}),
            }[kind]
            query = sa.update(model)
            if model is Audit:
                query = query.where(Audit.action == (D_ACTION if kind == "d-audit" else RECEIPT_KIND))
            owner.session.execute(query.values(**values))

    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "_leases", changed)
    with pytest.raises(ValueError):
        finalize(case)
    assert calls == [True, True] and all_state(case) == before and not f_rows(case)


def test_final_order_is_last_lease_then_complete_sql_then_deadline_and_commit(writer_case, monkeypatch):
    case = writer_case()
    persist(case)
    steps = []
    original_lease = CasdoorLeases.ensure_owned
    original_read = CasdoorInvitedLocalFinalizationRepository._reread
    from repositories import (
        casdoor_invited_local_finalization_repository_extend as module,
    )

    original_deadline = module._check_deadline

    def lease(owner, *args, **kwargs):
        steps.append("lease")
        return original_lease(owner, *args, **kwargs)

    def read(owner):
        steps.append("sql-start")
        original_read(owner)
        steps.append("sql-end")

    def deadline(value):
        steps.append("deadline")
        return original_deadline(value)

    def factory():
        session = case.factory()
        original = session.commit

        def commit():
            steps.append("commit")
            original()

        session.commit = commit
        return session

    monkeypatch.setattr(CasdoorLeases, "ensure_owned", lease)
    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "_reread", read)
    monkeypatch.setattr(module, "_check_deadline", deadline)
    finalize(case, factory=factory)
    assert steps[-7:] == ["deadline", "lease", "deadline", "sql-start", "sql-end", "deadline", "commit"]
    assert steps.count("lease") == 2 and steps.count("commit") == 1


@pytest.mark.parametrize("durable", [False, True])
def test_commit_ack_unknown_never_returns_or_retries_and_durable_effect_is_not_rewritten(
    writer_case, monkeypatch, durable
):
    case = writer_case()
    persist(case)
    before = all_state(case)
    appends, commits = [], []
    append = CasdoorAuditRepository._append_invited_finalization_receipt

    def appended(owner, *args, **kwargs):
        appends.append(True)
        return append(owner, *args, **kwargs)

    def factory():
        session = case.factory()
        commit = session.commit

        def unknown():
            commits.append(True)
            if durable:
                commit()
            raise RuntimeError("commit outcome unknown")

        session.commit = unknown
        return session

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", appended)
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        finalize(case, factory=factory)
    assert appends == [True] and commits == [True]
    assert len(f_rows(case)) == int(durable)
    if durable:
        after = all_state(case)
        with pytest.raises(ValueError):
            finalize(case)
        assert all_state(case) == after and appends == [True]
    else:
        assert all_state(case) == before


def schema_value():
    return {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "mode": MODE,
        "references": dict(
            zip(
                (
                    "operation_id",
                    "issuance_id",
                    "integration_id",
                    "namespace_id",
                    "revision_id",
                    "identity_id",
                    "account_id",
                    "workspace_id",
                    "invitation_join_id",
                ),
                (str(UUID(int=i)) for i in range(1, 10)),
                strict=True,
            )
        ),
        "generation": 1,
        "fence_epoch": 0,
        "write_receipt_sha256": "1" * 64,
        "before_postwrite_sha256": "2" * 64,
        "plan_sha256": "3" * 64,
        "finalized_ids": [],
        "finalized_rows_sha256": "4" * 64,
        "postwrite_sha256": "5" * 64,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate-key",
        "whitespace",
        "extra",
        "missing",
        "schema-bool",
        "schema-version",
        "kind",
        "mode",
        "generation-bool",
        "generation-zero",
        "generation-large",
        "generation-text",
        "fence-negative",
        "digest-upper",
        "digest-number",
        "refs-extra",
        "refs-missing",
        "uuid-upper",
        "uuid-short",
        "uuid-number",
        "ids-tuple",
        "ids-duplicate",
        "ids-order",
        "ids-large",
        "ids-nonuuid",
        "noncanonical-unicode",
        "oversized",
        "invalid-json",
        "nan",
        "raw-bytes",
    ],
)
def test_closed_f_schema_denies_ambiguous_primitive_noncanonical_or_oversized_values(kind):
    value = schema_value()
    raw = None
    if kind == "duplicate-key":
        raw = canonical(value).replace('"generation":1', '"generation":1,"generation":1')
    elif kind == "whitespace":
        raw = json.dumps(value)
    elif kind == "extra":
        value["email"] = "private@example.test"
    elif kind == "missing":
        del value["postwrite_sha256"]
    elif kind.startswith("schema-"):
        value["schema_version"] = {"schema-bool": True, "schema-version": 2}[kind]
    elif kind in ("kind", "mode"):
        value["receipt_kind" if kind == "kind" else "mode"] = D_ACTION
    elif kind.startswith("generation-"):
        value["generation"] = {
            "generation-bool": True,
            "generation-zero": 0,
            "generation-large": 2**63,
            "generation-text": "1",
        }[kind]
    elif kind == "fence-negative":
        value["fence_epoch"] = -1
    elif kind.startswith("digest-"):
        value["plan_sha256"] = "A" * 64 if kind == "digest-upper" else 0
    elif kind == "refs-extra":
        value["references"]["email"] = "private@example.test"
    elif kind == "refs-missing":
        del value["references"]["identity_id"]
    elif kind.startswith("uuid-"):
        value["references"]["identity_id"] = {
            "uuid-upper": "AAAAAAAA-0000-0000-0000-000000000003",
            "uuid-short": UUID(int=3).hex,
            "uuid-number": 3,
        }[kind]
    elif kind.startswith("ids-"):
        a, b = str(UUID(int=11)), str(UUID(int=12))
        value["finalized_ids"] = {
            "ids-tuple": (a,),
            "ids-duplicate": [a, a],
            "ids-order": [b, a],
            "ids-large": [str(UUID(int=i)) for i in range(101)],
            "ids-nonuuid": [True],
        }[kind]
        if kind == "ids-tuple":
            with pytest.raises(ValueError):
                InvitedLocalFinalizationReceipt.from_values(value)
            return
    elif kind == "noncanonical-unicode":
        raw = canonical(value).replace('"mode"', '"\\u006dode"')
    elif kind == "oversized":
        raw = " " * 32769
    elif kind == "invalid-json":
        raw = "{"
    elif kind == "nan":
        raw = canonical(value).replace('"generation":1', '"generation":NaN')
    elif kind == "raw-bytes":
        raw = canonical(value).encode()
    if raw is None:
        raw = canonical(value)
    with pytest.raises(ValueError):
        InvitedLocalFinalizationReceipt(raw)


def test_zero_ids_and_mapping_hash_domains_and_operation_binding_are_exact():
    operation = str(UUID(int=1))
    targets = (
        {
            "workspace_id": str(UUID(int=2)),
            "target_role": "normal",
            "builtin_id": "normal",
            "reason": "default_normal_fallback",
        },
    )
    plan_envelope = {"domain": PLAN_DOMAIN, "operation_id": operation, "targets": list(targets)}
    rows_envelope = {"domain": ROWS_DOMAIN, "operation_id": operation, "rows": []}
    assert mapping_plan_sha256(operation, targets) == hashlib.sha256(canonical(plan_envelope).encode()).hexdigest()
    assert finalized_rows_sha256(operation, ()) == hashlib.sha256(canonical(rows_envelope).encode()).hexdigest()
    assert finalized_rows_sha256(operation, ()) != mapping_plan_sha256(operation, targets)
    assert finalized_rows_sha256(str(UUID(int=3)), ()) != finalized_rows_sha256(operation, ())
    assert mapping_plan_sha256(str(UUID(int=3)), targets) != mapping_plan_sha256(operation, targets)


@pytest.mark.parametrize("kind", ["owner", "unknown", "builtin", "extra", "duplicate", "order", "role-graph", "reason"])
def test_mapping_plan_projection_denies_invalid_or_excess_detail(kind):
    row = {
        "workspace_id": str(UUID(int=2)),
        "target_role": "normal",
        "builtin_id": "normal",
        "reason": "default_normal_fallback",
    }
    rows = (row,)
    if kind in ("owner", "unknown"):
        row["target_role"] = "owner" if kind == "owner" else "unknown"
    elif kind == "builtin":
        row["builtin_id"] = "different"
    elif kind in ("extra", "role-graph"):
        row[kind] = "secret"
    elif kind == "reason":
        row["reason"] = "unknown"
    elif kind == "duplicate":
        rows = (row, dict(row))
    else:
        rows = (dict(row, workspace_id=str(UUID(int=3))), row)
    with pytest.raises(ValueError):
        mapping_plan_sha256(str(UUID(int=1)), rows)


@pytest.mark.parametrize("kind", ["parsed-copy", "d-observation", "object", "unprepared-owner", "wrong-session"])
def test_private_append_requires_exact_live_producer_capability(writer_case, kind):
    case = writer_case()
    persist(case)
    before = all_state(case)
    receipt = InvitedLocalFinalizationReceipt.from_values(schema_value())
    with case.db() as session, session.begin():
        owner = CasdoorInvitedLocalFinalizationRepository(session, configuration_factory)
        if kind == "object":
            owner = object()
        elif kind == "d-observation":
            from repositories.casdoor_invited_write_receipt_repository_extend import (
                CasdoorInvitedWriteReceiptRepository,
            )

            owner = CasdoorInvitedWriteReceiptRepository(session, configuration_factory).observe(case.attempt)
        elif kind in ("parsed-copy", "wrong-session"):
            owner.produce(case.attempt, roles=case.roles, leases=case.leases, deadline=case.deadline)
            if kind == "parsed-copy":
                receipt = InvitedLocalFinalizationReceipt(owner._receipt.canonical_json)
            else:
                other = case.factory()
                other.begin()
                try:
                    with pytest.raises(ValueError):
                        CasdoorAuditRepository(other)._append_invited_finalization_receipt(
                            owner._receipt, finalization_owner=owner
                        )
                finally:
                    other.rollback()
                    other.close()
                session.rollback()
                assert all_state(case) == before
                return
        with pytest.raises(ValueError):
            CasdoorAuditRepository(session)._append_invited_finalization_receipt(receipt, finalization_owner=owner)
        session.rollback()
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["autobegin", "used-root", "nested", "new", "dirty", "deleted"])
def test_unclean_supplied_session_rejected_before_e_owner_sql(writer_case, monkeypatch, kind):
    case = writer_case()
    persist(case)
    before = all_state(case)
    session = case.factory()
    if kind == "autobegin":
        session.execute(sa.select(1))
    elif kind == "used-root":
        session.begin()
    elif kind == "nested":
        session.begin()
        session.begin_nested()
    elif kind == "new":
        session.add(Audit(action="unrelated"))
    elif kind == "dirty":
        session.get(Account, case.ids["account"]).name = "changed"
    else:
        session.delete(session.get(Account, case.ids["account"]))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("E reached on unclean supplied Session")

    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "produce", forbidden)
    with pytest.raises(ValueError):
        finalize(case, factory=lambda: session)
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["duplicate", "oversize", "missing", "summary", "actor", "created", "namespace"])
def test_f_audit_current_exact_row_or_duplicate_fault_rolls_back(writer_case, monkeypatch, kind):
    from datetime import timedelta

    case = writer_case()
    persist(case)
    before = all_state(case)
    original = CasdoorAuditRepository._append_invited_finalization_receipt

    def changed(owner, *args, **kwargs):
        row = original(owner, *args, **kwargs)
        if kind == "duplicate":
            owner._session.execute(sa.insert(Audit).values(**dict(row._mapping, id=str(uuid4()))))
        elif kind == "missing":
            owner._session.execute(sa.delete(Audit).where(Audit.id == row.id))
        else:
            values = {
                "oversize": {"summary_json": "é" * 17000},
                "summary": {"summary_json": "{}"},
                "actor": {"actor_account_id": str(uuid4())},
                "created": {"created_at": row.created_at + timedelta(microseconds=1)},
                "namespace": {"namespace_id": str(uuid4())},
            }[kind]
            owner._session.execute(sa.update(Audit).where(Audit.id == row.id).values(**values))
        return row

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", changed)
    with pytest.raises(ValueError):
        finalize(case)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize("which", [1, 2])
def test_expired_deadline_after_initial_or_final_sql_reread_rolls_back(writer_case, monkeypatch, which):
    case = writer_case()
    persist(case)
    before = all_state(case)
    from repositories import (
        casdoor_invited_local_finalization_repository_extend as module,
    )

    original = module.CasdoorInvitedLocalFinalizationRepository._reread
    check = module._check_deadline
    reads = []

    def read(owner):
        original(owner)
        reads.append(True)

    def deadline(value):
        if len(reads) == which:
            raise ValueError("deadline expired")
        check(value)

    monkeypatch.setattr(module.CasdoorInvitedLocalFinalizationRepository, "_reread", read)
    monkeypatch.setattr(module, "_check_deadline", deadline)
    with pytest.raises(ValueError, match="deadline expired"):
        finalize(case)
    assert all_state(case) == before and not f_rows(case)


@pytest.mark.parametrize(
    "field",
    [
        "baseline_json",
        "last_applied_roles_json",
        "desired_roles_json",
        "last_applied_fingerprint",
        "created_at",
        "updated_at",
        "finalization",
    ],
)
def test_full_row_digest_exact_current_text_fingerprint_time_and_state(writer_case, field):
    from datetime import timedelta

    case = writer_case()
    persist(case)
    receipt = finalize(case)
    with case.db() as session:
        rows = tuple(session.execute(sa.select(*History.__table__.columns).order_by(History.id)))
    primitive = tuple({k: v.value if isinstance(v, StrEnum) else v for k, v in row._mapping.items()} for row in rows)
    projected = [
        dict(
            row,
            created_at=row["created_at"].isoformat(timespec="microseconds") + "Z",
            updated_at=row["updated_at"].isoformat(timespec="microseconds") + "Z",
        )
        for row in primitive
    ]
    operation = receipt.values()["references"]["operation_id"]
    envelope = {"domain": ROWS_DOMAIN, "operation_id": operation, "rows": projected}
    expected = hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()
    assert receipt.values()["finalized_rows_sha256"] == expected
    assert finalized_rows_sha256(operation, primitive) == expected
    altered = [dict(row) for row in primitive]
    if field in ("created_at", "updated_at"):
        # Keep the UTC/timestamp ordering valid while changing the exact value.
        altered[0][field] += timedelta(microseconds=-1 if field == "created_at" else 1)
        assert finalized_rows_sha256(operation, tuple(altered)) != expected
    elif field == "last_applied_fingerprint":
        altered[0][field] = "0" * 64
        assert finalized_rows_sha256(operation, tuple(altered)) != expected
    else:
        altered[0][field] = "pending" if field == "finalization" else "{}"
        with pytest.raises(ValueError):
            finalized_rows_sha256(operation, tuple(altered))


def test_f_receipt_byte_header_bound_precedes_full_row_materialization(writer_case, monkeypatch):
    case = writer_case()
    persist(case)
    before = all_state(case)
    original = CasdoorAuditRepository._append_invited_finalization_receipt
    statements = []

    def appended(owner, *args, **kwargs):
        row = original(owner, *args, **kwargs)
        owner._session.execute(sa.update(Audit).where(Audit.id == row.id).values(summary_json="é" * 17000))
        statements.clear()
        return row

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", appended)
    sa.event.listen(case.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ValueError):
            finalize(case)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture)
    # D audit full reads remain legitimate. F's exact readback rejects its length
    # header, so no full F SELECT is issued after its header.
    header_indices = [i for i, s in enumerate(statements) if s.startswith("SELECT casdoor_audit_extend.id, length")]
    assert header_indices
    suffix = statements[header_indices[-1] + 1 :]
    assert not any(s.startswith("SELECT") and "casdoor_audit_extend.summary_json" in s for s in suffix)
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["copied-owner", "copied-receipt"])
def test_live_append_capability_is_bound_to_exact_owner_and_receipt_identity(writer_case, monkeypatch, kind):
    from copy import copy

    from repositories.casdoor_invited_local_finalization_repository_extend import (
        _APPEND_PERMITS,
    )

    case = writer_case()
    persist(case)
    original = CasdoorAuditRepository._append_invited_finalization_receipt
    denied = []

    def guarded(owner, receipt, *, finalization_owner):
        copied_owner = copy(finalization_owner) if kind == "copied-owner" else finalization_owner
        copied_receipt = (
            InvitedLocalFinalizationReceipt(receipt.canonical_json) if kind == "copied-receipt" else receipt
        )
        with pytest.raises(ValueError):
            original(owner, copied_receipt, finalization_owner=copied_owner)
        denied.append(True)
        return original(owner, receipt, finalization_owner=finalization_owner)

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", guarded)
    finalize(case)
    assert denied == [True] and not _APPEND_PERMITS and len(f_rows(case)) == 1
