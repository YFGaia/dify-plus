"""Real D/F1 commits then independent F2; sequential offline SQLite only."""

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import RoleRef
from core.casdoor.invited_finalization_receipt import FULL_HISTORY_FIELDS, canonical
from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as F_ACTION
from core.casdoor.invited_write_receipt import RECEIPT_KIND as D_ACTION
from core.casdoor.leases import CasdoorLeases
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invited_finalization_receipt_repository_extend import (
    CasdoorInvitedFinalizationReceiptRepository,
    InvitedFinalizationObservation,
    _pending,
)
from repositories.casdoor_invited_local_finalization_repository_extend import CasdoorInvitedLocalFinalizationRepository
from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from services.casdoor_invited_finalization_receipt_service_extend import CasdoorInvitedFinalizationReceiptService
from services.casdoor_invited_write_receipt_service_extend import CasdoorInvitedWriteReceiptService
from sqlalchemy.exc import OperationalError

from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import configuration_factory
from tests.unit_tests.services.test_casdoor_invited_local_finalization_service_extend import all_state, f_rows, finalize
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import persist
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def observe(case, *, factory=None, roles=None, attempt=None):
    return CasdoorInvitedFinalizationReceiptService(
        session_factory=factory or case.factory, configuration_factory=configuration_factory
    ).observe_invited_finalization(attempt or case.attempt, roles=roles if roles is not None else case.roles)


def prepare(case, *, preserve="none", lost_ack=False):
    if preserve != "none":
        with case.db() as session, session.begin():
            workspaces = [str(case.configuration.workspace_mappings[0].workspace_id)]
            if preserve == "all":
                workspaces.append(str(case.configuration.default_workspace_id))
            for wid in set(workspaces):
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
    persist(case)
    if lost_ack:

        def factory():
            session = case.factory()
            commit = session.commit

            def uncertain():
                commit()
                raise RuntimeError("commit outcome unknown")

            session.commit = uncertain
            return session

        with pytest.raises(RuntimeError, match="commit outcome unknown"):
            finalize(case, factory=factory)
    else:
        finalize(case)  # The return value is deliberately discarded.


def assert_pending(call):
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$") as error:
        call()
    assert error.value.args == ("authorization_pending",) and error.value.__cause__ is None


def assert_denied_unchanged(case, **kwargs):
    before = all_state(case)
    assert_pending(lambda: observe(case, **kwargs))
    assert all_state(case) == before


def audit_row(case, action):
    with case.db() as session:
        return session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == action)).one()


def mutate_audit(case, action, **values):
    with case.db() as session, session.begin():
        session.execute(sa.update(Audit).where(Audit.action == action).values(**values))


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("invite_target", [False, True])
@pytest.mark.parametrize("preserve", ["none", "unmanaged", "owner", "all"])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_actual_committed_f1_fresh_read_no_writes_or_authority(
    writer_case, monkeypatch, absent, invite_target, preserve, lost_ack
):
    case = writer_case(absent=absent, invite_target=invite_target)
    prepare(case, preserve=preserve, lost_ack=lost_ack)
    before = all_state(case)
    steps, sql = [], []

    def forbidden(*_a, **_kw):
        raise AssertionError("F2 reached writer/receipt/lease/flush/commit")

    def factory():
        session = case.factory()
        session.commit = forbidden
        session.flush = forbidden
        original_begin, original_rollback, original_close = session.begin, session.rollback, session.close

        def begin(*a, **kw):
            steps.append("begin")
            return original_begin(*a, **kw)

        def rollback():
            steps.append("rollback")
            return original_rollback()

        def close():
            steps.append("close")
            return original_close()

        session.begin, session.rollback, session.close = begin, rollback, close
        return session

    monkeypatch.setattr(CasdoorLeases, "ensure_owned", forbidden)
    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "produce", forbidden)
    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "observe", forbidden)
    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_finalization_receipt", forbidden)
    sa.event.listen(case.engine, "before_cursor_execute", lambda _c, _u, statement, _p, _x, _m: sql.append(statement))
    result = observe(case, factory=factory)
    assert type(result) is InvitedFinalizationObservation
    assert result.operation_id == UUID(audit_row(case, F_ACTION).correlation_id)
    assert result.current_generation == 1
    assert (
        result.finalization_summary_sha256
        == hashlib.sha256(audit_row(case, F_ACTION).summary_json.encode()).hexdigest()
    )
    assert result.write_summary_sha256 == hashlib.sha256(audit_row(case, D_ACTION).summary_json.encode()).hexdigest()
    assert steps == ["begin", "rollback", "close"]
    assert all(s.lstrip().upper().startswith("SELECT") or s == "BEGIN" for s in sql)
    assert all_state(case) == before and len(f_rows(case)) == 1
    assert {f.name for f in fields(result)} == {
        "operation_id",
        "current_generation",
        "write_summary_sha256",
        "finalization_summary_sha256",
    }
    assert str(result.operation_id) not in repr(result) and result.finalization_summary_sha256 not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.current_generation = 0
    with pytest.raises((AttributeError, TypeError)):
        result.session = object()


@pytest.mark.parametrize("durable", [False, True])
def test_unknown_ack_no_f2_retry_and_failure_is_not_negative_commit_proof(writer_case, durable):
    case = writer_case()
    persist(case)
    commits = []

    def factory():
        session = case.factory()
        commit = session.commit

        def uncertain():
            commits.append(True)
            if durable:
                commit()
            raise RuntimeError("commit outcome unknown")

        session.commit = uncertain
        return session

    with pytest.raises(RuntimeError):
        finalize(case, factory=factory)
    before = all_state(case)
    if durable:
        assert observe(case).current_generation == 1
    else:
        assert_denied_unchanged(case)
    assert all_state(case) == before and commits == [True]


@pytest.mark.parametrize("action", [D_ACTION, F_ACTION])
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "action",
        "correlation",
        "actor",
        "result",
        "namespace",
        "revision",
        "identity",
        "account",
        "future",
        "before-operation",
        "oversized",
    ],
)
def test_exact_audit_row_missing_duplicate_headers_and_references(writer_case, action, kind):
    case = writer_case()
    prepare(case)
    row = audit_row(case, action)
    with case.db() as session, session.begin():
        if kind == "missing":
            session.execute(sa.delete(Audit).where(Audit.id == row.id))
        elif kind == "duplicate":
            session.execute(sa.insert(Audit).values(**dict(row._mapping, id=str(uuid4()))))
        else:
            changes = {
                "action": {"action": "other"},
                "correlation": {"correlation_id": str(uuid4())},
                "actor": {"actor_account_id": str(uuid4())},
                "result": {"result_code": "ok"},
                "namespace": {"namespace_id": str(uuid4())},
                "revision": {"revision_id": str(uuid4())},
                "identity": {"identity_id": str(uuid4())},
                "account": {"account_id": str(uuid4())},
                "future": {"created_at": row.created_at + timedelta(days=36500)},
                "before-operation": {"created_at": row.created_at - timedelta(days=1)},
                "oversized": {"summary_json": "é" * 17000},
            }[kind]
            session.execute(sa.update(Audit).where(Audit.id == row.id).values(**changes))
    assert_denied_unchanged(case)


@pytest.mark.parametrize("action", [D_ACTION, F_ACTION])
@pytest.mark.parametrize(
    "kind",
    [
        "json",
        "whitespace",
        "duplicate-key",
        "extra",
        "missing",
        "kind",
        "mode",
        "version",
        "fence",
        "generation",
        "refs",
        "unicode",
        "large-schema",
    ],
)
def test_canonical_schema_or_current_receipt_binding_denies(writer_case, action, kind):
    case = writer_case()
    prepare(case)
    row = audit_row(case, action)
    value = json.loads(row.summary_json)
    if kind == "json":
        raw = "{"
    elif kind == "whitespace":
        raw = json.dumps(value)
    elif kind == "duplicate-key":
        raw = row.summary_json[:-1] + ',"schema_version":1}'
    elif kind == "unicode":
        raw = row.summary_json.replace('"schema_version"', '"\\u0073chema_version"')
    else:
        if kind == "extra":
            value["unexpected"] = "secret"
        elif kind == "missing":
            del value["postwrite_sha256"]
        elif kind == "kind":
            value["receipt_kind"] = "unknown"
        elif kind == "mode":
            value["mode"] = "unknown"
        elif kind == "version":
            value["schema_version"] = True
        elif kind == "fence":
            value["fence_epoch"] += 1
        elif kind == "generation":
            value["generation" if action == F_ACTION else "generation_after"] += 1
        elif kind == "refs":
            value["references"]["identity_id"] = str(uuid4())
        elif kind == "large-schema":
            value["unexpected"] = "x" * 33000
        raw = canonical(value)
    mutate_audit(case, action, summary_json=raw)
    assert_denied_unchanged(case)


@pytest.mark.parametrize(
    "field",
    ["write_receipt_sha256", "before_postwrite_sha256", "plan_sha256", "finalized_rows_sha256", "postwrite_sha256"],
)
def test_each_f_digest_exact(writer_case, field):
    case = writer_case()
    prepare(case)
    value = json.loads(audit_row(case, F_ACTION).summary_json)
    value[field] = "0" * 64
    mutate_audit(case, F_ACTION, summary_json=canonical(value))
    assert_denied_unchanged(case)


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "issuance_id",
        "integration_id",
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "workspace_id",
        "invitation_join_id",
    ],
)
def test_each_f_reference_exact(writer_case, field):
    case = writer_case()
    prepare(case)
    value = json.loads(audit_row(case, F_ACTION).summary_json)
    value["references"][field] = str(uuid4())
    mutate_audit(case, F_ACTION, summary_json=canonical(value))
    assert_denied_unchanged(case)


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate", "order", "pending", "unrecorded", "missing-row"])
def test_exact_full_history_and_id_set(writer_case, kind):
    case = writer_case()
    prepare(case)
    value = json.loads(audit_row(case, F_ACTION).summary_json)
    with case.db() as session, session.begin():
        if kind == "pending":
            session.execute(
                sa.update(History).where(History.id == value["finalized_ids"][0]).values(finalization="pending")
            )
        elif kind == "missing-row":
            session.execute(sa.delete(History).where(History.id == value["finalized_ids"][0]))
        elif kind == "unrecorded":
            row = session.execute(sa.select(*History.__table__.columns).limit(1)).one()
            session.execute(
                sa.insert(History).values(
                    **dict(
                        row._mapping,
                        id=str(uuid4()),
                        workspace_id=value["references"]["workspace_id"],
                        join_id=value["references"]["invitation_join_id"],
                    )
                )
            )
        else:
            if kind == "missing":
                value["finalized_ids"].pop()
            elif kind == "extra":
                value["finalized_ids"] = sorted(value["finalized_ids"] + [str(uuid4())])
            elif kind == "duplicate":
                value["finalized_ids"].append(value["finalized_ids"][0])
            else:
                value["finalized_ids"].reverse()
            session.execute(sa.update(Audit).where(Audit.action == F_ACTION).values(summary_json=canonical(value)))
    assert_denied_unchanged(case)


@pytest.mark.parametrize("field", FULL_HISTORY_FIELDS)
def test_every_f_bound_full_history_column_drift_denies(writer_case, field):
    case = writer_case()
    prepare(case)
    with case.db() as session, session.begin():
        row = session.execute(sa.select(*History.__table__.columns).limit(1)).one()
        if field.endswith("_id") or field == "id":
            value = str(uuid4())
        elif field in ("created_at", "updated_at"):
            value = getattr(row, field) + timedelta(microseconds=-1 if field == "created_at" else 1)
        elif field in ("ownership_epoch", "desired_generation"):
            value = getattr(row, field) + 1
        elif field == "ownership":
            value = "local_override"
        elif field == "source":
            value = "fallback" if str(row.source) == "mapping" else "mapping"
        elif field == "tombstone":
            value = True
        elif field == "finalization":
            value = "pending"
        elif field == "last_applied_fingerprint":
            value = "0" * 64
        else:
            value = "{}"
        session.execute(sa.update(History).where(History.id == row.id).values(**{field: value}))
    assert_denied_unchanged(case)


@pytest.mark.parametrize(
    "kind",
    [
        "generation",
        "join-role",
        "join-missing",
        "intent-missing",
        "extra-intent",
        "intent-proof",
        "integration",
        "namespace",
        "workspace",
        "revision",
        "account",
    ],
)
def test_current_scope_and_invitation_barriers(writer_case, kind):
    case = writer_case()
    prepare(case)
    with case.db() as session, session.begin():
        if kind == "generation":
            session.execute(sa.update(Identity).values(sync_generation=2))
        elif kind == "join-role":
            session.execute(sa.update(Join).values(role="dataset_operator"))
        elif kind == "join-missing":
            session.execute(sa.delete(Join).where(Join.tenant_id == str(case.attempt.workspace_id)))
        elif kind == "intent-missing":
            session.execute(sa.delete(Intent))
        elif kind == "extra-intent":
            row = session.execute(sa.select(*Intent.__table__.columns).limit(1)).one()
            session.execute(sa.insert(Intent).values(**dict(row._mapping, id=str(uuid4()), idempotency_key="another")))
        elif kind == "intent-proof":
            session.execute(sa.update(Intent).values(proof_ref="invalid"))
        elif kind == "integration":
            session.execute(sa.update(Integration).values(enabled=False))
        elif kind == "namespace":
            session.execute(sa.update(Namespace).values(lifecycle="archived"))
        elif kind == "workspace":
            session.execute(sa.update(Tenant).values(status="archive"))
        elif kind == "revision":
            session.execute(sa.update(Revision).values(config_digest="0" * 64))
        else:
            session.execute(sa.update(Account).values(status="banned"))
    assert_denied_unchanged(case)


@pytest.mark.parametrize(
    "kind",
    ["plan", "subject", "owner", "user-name", "duplicate", "many", "roles-list", "role-owner", "role-name", "object"],
)
def test_current_verified_roles_required_and_full_ordered_plan_exact(writer_case, kind):
    case = writer_case()
    prepare(case)
    changes = {
        "plan": {"effective_roles": ()},
        "subject": {"subject": "other"},
        "owner": {"user_ref": StructuredUserRef("other", "person")},
        "user-name": {"user_ref": StructuredUserRef("OfflineOrg", "")},
        "duplicate": {"effective_roles": case.roles.effective_roles * 2},
        "many": {"effective_roles": case.roles.effective_roles * 2001},
        "roles-list": {"effective_roles": list(case.roles.effective_roles)},
        "role-owner": {"effective_roles": (RoleRef(organization="other", name="operators"),)},
        "role-name": {"effective_roles": (RoleRef.model_construct(organization="OfflineOrg", name=""),)},
    }
    roles = object() if kind == "object" else replace(case.roles, **changes[kind])
    assert_denied_unchanged(case, roles=roles)


def test_role_graph_difference_with_identical_plan_is_only_plan_equality(writer_case):
    case = writer_case()
    prepare(case)
    roles = replace(
        case.roles, effective_roles=case.roles.effective_roles + (RoleRef(organization="OfflineOrg", name="unused"),)
    )
    before = all_state(case)
    assert observe(case, roles=roles).current_generation == 1
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["autobegin", "used-root", "nested", "new", "dirty", "deleted", "inactive"])
def test_unclean_service_session_denied_before_repository(writer_case, monkeypatch, kind):
    case = writer_case()
    prepare(case)
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
    elif kind == "deleted":
        session.delete(session.get(Account, case.ids["account"]))
    else:
        session.begin()
        session.get_transaction()._state = sa.orm.session.SessionTransactionState.DEACTIVE
    monkeypatch.setattr(
        CasdoorInvitedFinalizationReceiptRepository, "observe", lambda *_a, **_k: pytest.fail("repository reached")
    )
    assert_denied_unchanged(case, factory=lambda: session)


@pytest.mark.parametrize("kind", ["autobegin", "used-root", "nested", "new", "dirty", "deleted"])
def test_repository_requires_first_explicit_clean_root(writer_case, kind):
    case = writer_case()
    prepare(case)
    before = all_state(case)
    with case.factory() as session:
        if kind == "autobegin":
            session.execute(sa.select(1))
        else:
            session.begin()
            if kind == "used-root":
                session.execute(sa.select(1))
            elif kind == "nested":
                session.begin_nested()
            elif kind == "new":
                session.add(Audit(action="unrelated"))
            elif kind == "dirty":
                session.get(Account, case.ids["account"]).name = "changed"
            else:
                session.delete(session.get(Account, case.ids["account"]))
        reader = CasdoorInvitedFinalizationReceiptRepository(session, configuration_factory)
        assert_pending(lambda: reader.observe(case.attempt, roles=case.roles))
        session.rollback()
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["scope", "history", "d-audit", "f-audit", "parents", "expanded-parent", "nowait"])
def test_first_last_full_readback_drift_or_lock_error_fails(writer_case, monkeypatch, kind):
    case = writer_case()
    prepare(case)
    before = all_state(case)
    injected = []
    if kind == "expanded-parent":
        original = CasdoorLoginScopeRepository._lock_invited_candidate_parents

        def lock(owner, context, candidate):
            original(owner, context, candidate)
            tenant = owner.session.execute(sa.select(*Tenant.__table__.columns).limit(1)).one()
            wid = str(uuid4())
            owner.session.execute(sa.insert(Tenant).values(**dict(tenant._mapping, id=wid)))
            owner.session.execute(
                sa.insert(Join).values(
                    id=str(uuid4()), account_id=case.ids["account"], tenant_id=wid, role="normal", current=False
                )
            )
            injected.append(True)

        monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_candidate_parents", lock)
    elif kind == "nowait":

        def blocked(*_a, **_k):
            injected.append(True)
            raise OperationalError("lock NOWAIT", {}, Exception("sensitive driver detail"))

        monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_integration", blocked)
    else:
        original = CasdoorInvitedFinalizationReceiptRepository._verify
        calls = []

        def verify(owner, *a, **kw):
            result = original(owner, *a, **kw)
            calls.append(True)
            if len(calls) == 1:
                model, values = {
                    "scope": (Identity, {"sync_generation": 2}),
                    "history": (History, {"updated_at": sa.func.datetime("now")}),
                    "d-audit": (Audit, {"result_code": "drift"}),
                    "f-audit": (Audit, {"id": str(uuid4())}),
                    "parents": (Account, {"name": "drift"}),
                }[kind]
                query = sa.update(model).values(**values)
                if kind in ("d-audit", "f-audit"):
                    query = query.where(Audit.action == (D_ACTION if kind == "d-audit" else F_ACTION))
                owner.session.execute(query)
                injected.append(True)
            return result

        monkeypatch.setattr(CasdoorInvitedFinalizationReceiptRepository, "_verify", verify)
    assert_denied_unchanged(case)
    assert injected == [True] and all_state(case) == before


@pytest.mark.parametrize("method", ["rollback", "close"])
def test_lifecycle_failure_suppresses_success_in_fixed_safe_shape(writer_case, method):
    case = writer_case()
    prepare(case)

    def factory():
        session = case.factory()
        original = getattr(session, method)

        def fault():
            original()
            raise RuntimeError("sensitive lifecycle failure")

        setattr(session, method, fault)
        return session

    assert_denied_unchanged(case, factory=factory)


def test_projection_is_immutable_exact_ids_and_leaves_timestamps_and_database(writer_case):
    case = writer_case()
    prepare(case)
    before = all_state(case)
    with case.db() as session:
        row = session.execute(sa.select(*History.__table__.columns).limit(1)).one()
    copy = _pending(row, {row.id})
    assert type(copy._mapping) is MappingProxyType
    assert dict(copy._mapping) == dict(row._mapping, finalization=CasdoorFinalizationState.PENDING)
    with pytest.raises(TypeError):
        copy._mapping["updated_at"] = None
    with pytest.raises((FrozenInstanceError, TypeError)):
        copy.finalization = CasdoorFinalizationState.PENDING
    assert _pending(row, set()) is row and row.finalization is CasdoorFinalizationState.FINALIZED
    observe(case)
    assert all_state(case) == before


def test_old_e_reader_remains_strict_and_f2_value_is_not_authority(writer_case):
    case = writer_case()
    prepare(case)
    before = all_state(case)
    result = observe(case)
    assert_pending(
        lambda: CasdoorInvitedWriteReceiptService(
            session_factory=case.factory, configuration_factory=configuration_factory
        ).observe_invited_write_receipt(case.attempt)
    )
    assert_denied_unchanged(case, attempt=result)
    assert all_state(case) == before


@pytest.mark.parametrize("kind", ["account-profile", "join-metadata", "audit-id", "audit-time"])
def test_historical_unbound_fields_checked_current_not_commit_moment(writer_case, kind):
    case = writer_case()
    prepare(case)
    with case.db() as session, session.begin():
        if kind == "account-profile":
            session.execute(sa.update(Account).values(name="current profile"))
        elif kind == "join-metadata":
            session.execute(sa.update(Join).values(current=False))
        elif kind == "audit-id":
            session.execute(sa.update(Audit).where(Audit.action == F_ACTION).values(id=str(uuid4())))
        else:
            row = session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == F_ACTION)).one()
            session.execute(
                sa.update(Audit).where(Audit.id == row.id).values(created_at=row.created_at - timedelta(microseconds=1))
            )
    before = all_state(case)
    assert observe(case).current_generation == 1 and all_state(case) == before


@pytest.mark.parametrize("action", [D_ACTION, F_ACTION])
def test_oversize_audit_header_precedes_summary_materialization(writer_case, action):
    case = writer_case()
    prepare(case)
    mutate_audit(case, action, summary_json="é" * 17000)
    statements = []

    def capture(_c, _u, statement, parameters, _x, _m):
        statements.append((statement, parameters))

    sa.event.listen(case.engine, "before_cursor_execute", capture)
    try:
        assert_pending(lambda: observe(case))
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture)
    selected = [s for s, p in statements if s.startswith("SELECT") and action in p]
    assert selected and any("length(CAST" in s for s in selected)
    assert not any("casdoor_audit_extend.summary_json" in s and "length(CAST" not in s for s in selected)


@pytest.mark.parametrize(
    "kind", ["object", "generation-bool", "generation-negative", "generation-large", "workspace", "issuance"]
)
def test_closed_attempt_input_denied_before_session_creation(writer_case, kind):
    case = writer_case()
    attempts = {
        "generation-bool": replace(case.attempt, expected_generation=True),
        "generation-negative": replace(case.attempt, expected_generation=-1),
        "generation-large": replace(case.attempt, expected_generation=2**63),
        "workspace": replace(case.attempt, workspace_id=str(case.attempt.workspace_id)),
        "issuance": replace(case.attempt, issuance_id=None),
    }
    attempt = object() if kind == "object" else attempts[kind]

    def forbidden():
        pytest.fail("invalid input reached Session factory")

    assert_pending(lambda: observe(case, factory=forbidden, attempt=attempt))


@pytest.mark.parametrize("preserve", ["none", "all"])
def test_current_plan_hash_required_even_when_no_managed_history(writer_case, preserve):
    case = writer_case()
    prepare(case, preserve=preserve)
    before = all_state(case)
    if preserve == "all":
        assert not before["casdoor_managed_membership_extend"]
    assert_denied_unchanged(case, roles=replace(case.roles, effective_roles=()))
    with case.db() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 1


@pytest.mark.parametrize("consumer", ["f1", "b3", "guard", "intent", "append"])
def test_observation_cannot_be_used_by_existing_authority_owners(writer_case, consumer):
    from repositories.casdoor_invited_login_guard_repository_extend import _binding
    from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
    from services.casdoor_invited_local_finalization_service_extend import CasdoorInvitedLocalFinalizationService

    case = writer_case()
    prepare(case)
    observation = observe(case)
    before = all_state(case)
    if consumer == "f1":
        service = CasdoorInvitedLocalFinalizationService(
            session_factory=case.factory, configuration_factory=configuration_factory
        )
        with pytest.raises(ValueError):
            service.finalize_invited_local_memberships(
                observation, roles=case.roles, leases=case.leases, deadline=case.deadline
            )
    elif consumer == "b3":
        with pytest.raises(ValueError):
            case.writer.persist_invited_local_memberships(
                observation, roles=case.roles, leases=case.leases, deadline=case.deadline
            )
    else:
        with case.factory() as session, session.begin():
            with pytest.raises(ValueError):
                if consumer == "guard":
                    _binding(observation, session)
                elif consumer == "intent":
                    CasdoorRequiredIntentRepository(session).read_locked(
                        case.attempt.account_id, case.attempt.workspace_id, invitation_guard=observation
                    )
                else:
                    CasdoorAuditRepository(session)._append_invited_finalization_receipt(
                        observation, finalization_owner=observation
                    )
    assert all_state(case) == before
