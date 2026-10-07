"""Real P3L/B3 receipt then fresh SQL-only observation; offline SQLite only."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.invited_write_receipt import RECEIPT_KIND, InvitedLocalWriteReceipt
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invitation_finalization_repository_extend import CasdoorInvitationFinalizationRepository
from repositories.casdoor_invited_login_guard_repository_extend import _REGISTRY
from repositories.casdoor_invited_write_receipt_repository_extend import (
    CasdoorInvitedWriteReceiptRepository,
    InvitedWriteReceiptObservation,
)
from services.casdoor_invited_write_receipt_service_extend import CasdoorInvitedWriteReceiptService
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import configuration_factory
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import database, persist
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def all_audits(case):
    with case.db() as session:
        return tuple(session.execute(sa.select(*Audit.__table__.columns).order_by(Audit.id)))


def receipt_row(case):
    with case.db() as session:
        return session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == RECEIPT_KIND)).one()


def reader(case, *, factory=None):
    return CasdoorInvitedWriteReceiptService(
        session_factory=factory or case.factory,
        configuration_factory=configuration_factory,
    )


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("invite_target", [False, True])
def test_real_completed_writer_is_read_from_fresh_root_without_authority(
    writer_case, monkeypatch, absent, invite_target
):
    case = writer_case(absent=absent, invite_target=invite_target)
    result = persist(case)
    assert not _REGISTRY
    assert all(not session.in_transaction() for session in case.sessions)
    before, audits = database(case), all_audits(case)
    expected = receipt_row(case)
    producer_sessions = tuple(case.sessions)
    statements, commits = [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("writer, Redis or old-generation inspection called")

    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "inspect", forbidden)
    monkeypatch.setattr(CasdoorInvitationFinalizationRepository, "_scope", forbidden)
    monkeypatch.setattr(CasdoorLocalMembershipService, "persist_local_memberships", forbidden)
    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", forbidden)
    monkeypatch.setattr(case.lease_redis, "eval", forbidden)
    monkeypatch.setattr(case.redis, "eval", forbidden)

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def committed(_connection):
        commits.append(True)

    sa.event.listen(case.engine, "before_cursor_execute", capture)
    sa.event.listen(case.engine, "commit", committed)
    try:
        observation = reader(case).observe_invited_write_receipt(case.attempt)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture)
        sa.event.remove(case.engine, "commit", committed)
    assert type(observation) is InvitedWriteReceiptObservation
    assert observation.receipt == InvitedLocalWriteReceipt(expected.summary_json)
    assert observation.operation_id == case.pending.snapshot.operation_id
    assert observation.completion_proof_ref == case.completed.facts.proof_ref
    assert observation.current_generation == result.generation == 1
    assert {f.name for f in fields(observation)} == {
        "receipt",
        "operation_id",
        "completion_proof_ref",
        "current_generation",
    }
    assert "ExactSubject" not in repr(observation)
    assert not hasattr(observation, "audit_id")
    assert not hasattr(observation, "audit_created_at")
    assert len(case.sessions) == len(producer_sessions) + 1
    assert case.sessions[-1] not in producer_sessions
    assert not case.sessions[-1].in_transaction()
    assert commits == []
    assert all(s.startswith("SELECT") or s == "BEGIN" for s in statements)
    assert database(case) == before
    assert all_audits(case) == audits
    assert not _REGISTRY
    with pytest.raises(FrozenInstanceError):
        observation.current_generation = 2


def test_completed_operation_does_not_expire_after_seven_days(writer_case, monkeypatch):
    from repositories import casdoor_invited_write_receipt_repository_extend as module

    case = writer_case(absent=True)
    persist(case)
    with case.db() as session:
        terminated = session.scalar(sa.select(Intent.terminated_at))
    monkeypatch.setattr(module, "_now", lambda: terminated + timedelta(days=8))
    assert reader(case).observe_invited_write_receipt(case.attempt).current_generation == 1


def current_state(case):
    return database(case), all_audits(case)


def assert_denied_without_writes(case):
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict

    before = current_state(case)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$"):
        reader(case).observe_invited_write_receipt(case.attempt)
    assert current_state(case) == before
    assert not _REGISTRY


@pytest.mark.parametrize(
    "column",
    [
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "actor_account_id",
        "action",
        "result_code",
        "correlation_id",
        "summary_json",
        "id",
        "created_at",
    ],
)
def test_audit_bound_fields_or_invalid_unbound_format_are_rejected(writer_case, column):
    case = writer_case()
    persist(case)
    row = receipt_row(case)
    if column.endswith("_id"):
        bad = str(uuid4())
    elif column == "id":
        bad = "invalid-uuid"
    elif column == "created_at":
        bad = row.created_at + timedelta(days=1)
    else:
        bad = "changed"
    with case.db() as session, session.begin():
        session.execute(sa.update(Audit).where(Audit.id == row.id).values({column: bad}))
    assert_denied_without_writes(case)


@pytest.mark.parametrize("kind", ["delete", "duplicate", "noncanonical", "oversized", "duplicate-json-key"])
def test_missing_ambiguous_or_malformed_receipt_rejects(writer_case, kind):
    case = writer_case()
    persist(case)
    row = receipt_row(case)
    with case.db() as session, session.begin():
        if kind == "delete":
            session.execute(sa.delete(Audit).where(Audit.id == row.id))
        elif kind == "duplicate":
            session.execute(sa.insert(Audit).values(**dict(row._mapping, id=str(uuid4()))))
        else:
            raw = {
                "noncanonical": row.summary_json + " ",
                "oversized": "é" * 16385,
                "duplicate-json-key": row.summary_json.replace(
                    '"schema_version":1', '"schema_version":1,"schema_version":1'
                ),
            }[kind]
            session.execute(sa.update(Audit).where(Audit.id == row.id).values(summary_json=raw))
    assert_denied_without_writes(case)


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
def test_every_receipt_reference_must_bind_actual_rows(writer_case, field):
    import json

    case = writer_case()
    persist(case)
    row = receipt_row(case)
    data = InvitedLocalWriteReceipt(row.summary_json).values()
    data["references"][field] = str(uuid4())
    with case.db() as session, session.begin():
        session.execute(
            sa.update(Audit)
            .where(Audit.id == row.id)
            .values(
                summary_json=json.dumps(data, sort_keys=True, separators=(",", ":")),
            )
        )
    assert_denied_without_writes(case)


@pytest.mark.parametrize(
    "field",
    [
        "generation_before",
        "generation_after",
        "fence_epoch",
        "scope_digest",
        "completion_proof_ref",
        "payload_digest",
        "postwrite_sha256",
    ],
)
def test_receipt_generation_proof_and_digests_must_match_current_binding(writer_case, field):
    import json

    case = writer_case()
    persist(case)
    row = receipt_row(case)
    data = InvitedLocalWriteReceipt(row.summary_json).values()
    data[field] = data[field] + 1 if type(data[field]) is int else "0" * 64
    with case.db() as session, session.begin():
        session.execute(
            sa.update(Audit)
            .where(Audit.id == row.id)
            .values(
                summary_json=json.dumps(data, sort_keys=True, separators=(",", ":")),
            )
        )
    assert_denied_without_writes(case)


@pytest.mark.parametrize(
    "field",
    [
        "workspace_id",
        "ownership_decision",
        "intent_barrier",
        "outcome",
        "join_id",
        "membership_id",
        "current_role",
        "membership_created",
        "role_changed",
        "metadata_changed",
        "membership_regranted",
    ],
)
def test_each_checkable_stored_result_field_rejects_inconsistency(writer_case, field):
    import json

    case = writer_case()
    persist(case)
    row = receipt_row(case)
    data = InvitedLocalWriteReceipt(row.summary_json).values()
    item = data["results"][0]
    if field.endswith("_id"):
        item[field] = str(uuid4())
    elif type(item[field]) is bool:
        item[field] = not item[field]
    else:
        item[field] = {
            "ownership_decision": "preserve_unmanaged",
            "intent_barrier": "pending",
            "outcome": "noop",
            "current_role": "owner",
        }[field]
    with case.db() as session, session.begin():
        session.execute(
            sa.update(Audit)
            .where(Audit.id == row.id)
            .values(
                summary_json=json.dumps(data, sort_keys=True, separators=(",", ":")),
            )
        )
    assert_denied_without_writes(case)


@pytest.mark.parametrize("generation", [0, 2])
def test_current_identity_must_be_exactly_original_generation_plus_one(writer_case, generation):
    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        session.execute(sa.update(Identity).values(sync_generation=generation))
    assert_denied_without_writes(case)


@pytest.mark.parametrize("column", list(Intent.__table__.columns.keys()))
def test_every_invitation_operation_column_is_reconstructed_or_checked(writer_case, column, request):
    from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import changed_value

    case = writer_case()
    persist(case)
    try:
        with case.db() as session, session.begin():
            row = session.execute(sa.select(*Intent.__table__.columns)).one()
            session.execute(
                sa.update(Intent).values({column: changed_value(Intent.__table__.c[column], getattr(row, column))})
            )
    except (ValueError, sa.exc.SQLAlchemyError):
        request.node.user_properties.append(("rejection_owner", "schema_or_bind_constraint"))
        return
    request.node.user_properties.append(("rejection_owner", "fresh_reader"))
    assert_denied_without_writes(case)


@pytest.mark.parametrize(
    "column",
    [
        "id",
        "namespace_id",
        "identity_id",
        "account_id",
        "workspace_id",
        "join_id",
        "ownership",
        "ownership_epoch",
        "source",
        "desired_generation",
        "revision_id",
        "finalization",
        "tombstone",
    ],
)
def test_every_digest_covered_history_column_drift_rejects(writer_case, column, request):
    from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import changed_value

    case = writer_case()
    persist(case)
    try:
        with case.db() as session, session.begin():
            row = session.execute(sa.select(*History.__table__.columns).order_by(History.id).limit(1)).one()
            session.execute(
                sa.update(History)
                .where(History.id == row.id)
                .values(
                    {
                        column: changed_value(History.__table__.c[column], getattr(row, column)),
                    }
                )
            )
    except (ValueError, sa.exc.SQLAlchemyError):
        request.node.user_properties.append(("rejection_owner", "schema_or_bind_constraint"))
        return
    request.node.user_properties.append(("rejection_owner", "fresh_reader"))
    assert_denied_without_writes(case)


@pytest.mark.parametrize(
    "column", ["baseline_json", "last_applied_roles_json", "desired_roles_json", "last_applied_fingerprint"]
)
@pytest.mark.parametrize("kind", ["malformed", "oversized"])
def test_excluded_history_fields_only_check_current_bounded_consistency(writer_case, column, kind):
    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        session.execute(sa.update(History).values({column: "{}" if kind == "malformed" else "x" * 16385}))
    assert_denied_without_writes(case)


@pytest.mark.parametrize(
    "kind",
    [
        "account-status",
        "account-email",
        "uninitialized",
        "normalized-email",
        "identity-subject",
        "identity-account",
        "identity-namespace",
        "extra-identity",
        "missing-identity",
        "integration-disabled",
        "revision",
        "fence",
        "workspace",
        "join-role",
        "join-id",
        "extra-join",
        "missing-join",
        "lifecycle-state",
        "lifecycle-epoch",
        "issuance-payload",
        "issuance-receipt",
        "extra-history",
        "missing-history",
    ],
)
def test_current_authority_and_complete_postwrite_scope_drift_rejects(writer_case, kind):
    from models.casdoor_extend import CasdoorIntegrationExtend as Integration
    from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
    from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
    from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle

    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        updates = {
            "account-status": (Account, {"status": "banned"}),
            "account-email": (Account, {"email": "changed@example.test"}),
            "uninitialized": (Account, {"initialized_at": None}),
            "normalized-email": (Account, {"normalized_email": "wrong@example.test"}),
            "identity-subject": (Identity, {"subject": "changed"}),
            "identity-account": (Identity, {"account_id": str(uuid4())}),
            "identity-namespace": (Identity, {"namespace_id": str(uuid4())}),
            "integration-disabled": (Integration, {"enabled": False}),
            "revision": (Integration, {"active_revision_id": str(uuid4())}),
            "fence": (Namespace, {"fence_epoch": 3}),
            "workspace": (Tenant, {"status": "archive"}),
            "join-role": (Join, {"role": "owner"}),
            "join-id": (Join, {"id": str(uuid4())}),
            "lifecycle-state": (Lifecycle, {"state": "withdrawn"}),
            "lifecycle-epoch": (Lifecycle, {"epoch": 500}),
            "issuance-payload": (Issuance, {"payload_json": "{}"}),
            "issuance-receipt": (Issuance, {"consumption_receipt_json": "{}"}),
        }
        if kind in updates:
            model, values = updates[kind]
            query = sa.update(model)
            if model is Join:
                query = query.where(Join.id == case.completed.facts.join_id)
            session.execute(query.values(**values))
        elif kind.startswith("missing-"):
            model = {"missing-identity": Identity, "missing-join": Join, "missing-history": History}[kind]
            session.execute(sa.delete(model))
        else:
            model = {"extra-identity": Identity, "extra-join": Join, "extra-history": History}[kind]
            row = dict(session.execute(sa.select(*model.__table__.columns).limit(1)).one()._mapping)
            values = {"id": str(uuid4())}
            if model is Identity:
                values["namespace_id"] = str(uuid4())
            elif model is Join:
                values["tenant_id"] = str(uuid4())
            else:
                values["workspace_id"] = str(uuid4())
            session.execute(sa.insert(model).values(**dict(row, **values)))
    assert_denied_without_writes(case)


@pytest.mark.parametrize("kind", ["own", "null-workspace", "other-namespace", "other-state"])
def test_additional_non_avatar_intent_is_never_hidden_or_exempted(writer_case, kind):
    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        row = dict(session.execute(sa.select(*Intent.__table__.columns)).one()._mapping)
        row.update(id=str(uuid4()), idempotency_key=uuid4().hex * 2)
        if kind == "null-workspace":
            row["workspace_id"] = None
        elif kind == "other-namespace":
            row["namespace_id"] = str(uuid4())
        elif kind == "other-state":
            row["operation_state"] = "unknown"
        session.execute(sa.insert(Intent).values(**row))
    assert_denied_without_writes(case)


@pytest.mark.parametrize("kind", ["audit-id", "audit-time", "omitted-preserved-result"])
def test_documented_unbound_history_limits_preserve_stored_facts_without_authenticity_claim(writer_case, kind):
    import json

    case = writer_case()
    if kind == "omitted-preserved-result":
        mapped = case.configuration.workspace_mappings[0].workspace_id
        with case.db() as session, session.begin():
            session.add(Join(account_id=case.ids["account"], tenant_id=str(mapped), role="normal", current=False))
    persist(case)
    row = receipt_row(case)
    with case.db() as session, session.begin():
        if kind == "audit-id":
            session.execute(sa.update(Audit).where(Audit.id == row.id).values(id=str(uuid4())))
        elif kind == "audit-time":
            session.execute(
                sa.update(Audit).where(Audit.id == row.id).values(created_at=row.created_at + timedelta(microseconds=1))
            )
        else:
            value = InvitedLocalWriteReceipt(row.summary_json).values()
            assert any(r["outcome"] == "preserved" for r in value["results"])
            value["results"] = [r for r in value["results"] if r["outcome"] != "preserved"]
            session.execute(
                sa.update(Audit)
                .where(Audit.id == row.id)
                .values(summary_json=json.dumps(value, sort_keys=True, separators=(",", ":")))
            )
    stored = receipt_row(case)
    before = current_state(case)
    observation = reader(case).observe_invited_write_receipt(case.attempt)
    assert observation.receipt.canonical_json == stored.summary_json
    assert current_state(case) == before
    assert not hasattr(observation, "audit_id")
    assert not hasattr(observation, "audit_created_at")


def test_parent_locks_precede_children_and_no_late_parent_locks(writer_case, monkeypatch):
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository

    case = writer_case()
    persist(case)
    steps = []
    originals = {
        name: getattr(CasdoorLoginScopeRepository, name)
        for name in (
            "_project_scope",
            "_lock_invited_integration",
            "_lock_invited_candidate_parents",
        )
    }

    def wrapped(name):
        def call(owner, *args, **kwargs):
            steps.append(name)
            return originals[name](owner, *args, **kwargs)

        return call

    for name in originals:
        monkeypatch.setattr(CasdoorLoginScopeRepository, name, wrapped(name))
    original_one = CasdoorInvitedWriteReceiptRepository._one

    def one(owner, model, *args, **kwargs):
        steps.append((model.__tablename__, kwargs.get("lock")))
        return original_one(owner, model, *args, **kwargs)

    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "_one", one)
    reader(case).observe_invited_write_receipt(case.attempt)
    assert steps[:5] == [
        "_project_scope",
        "_lock_invited_integration",
        "_project_scope",
        "_lock_invited_candidate_parents",
        "_project_scope",
    ]
    assert steps[5:10] == [
        (name, True)
        for name in (
            "invitation_authority_lifecycle_extend",
            "invitation_authority_issuance_extend",
            "tenant_account_joins",
            "casdoor_sync_intent_extend",
            "casdoor_audit_extend",
        )
    ]
    assert steps[10] == "_project_scope"
    assert steps[11:] == [
        (name, False)
        for name in (
            "invitation_authority_lifecycle_extend",
            "invitation_authority_issuance_extend",
            "tenant_account_joins",
            "casdoor_sync_intent_extend",
            "casdoor_audit_extend",
        )
    ]


@pytest.mark.parametrize("after_parents", [False, True])
def test_parent_expansion_is_rejected_without_late_locking(writer_case, monkeypatch, after_parents):
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository

    case = writer_case()
    persist(case)
    before = current_state(case)
    name = "_lock_invited_candidate_parents" if after_parents else "_lock_invited_integration"
    original = getattr(CasdoorLoginScopeRepository, name)
    parent = CasdoorLoginScopeRepository._lock_invited_candidate_parents
    calls = []

    def count_parent(owner, *args, **kwargs):
        calls.append(True)
        return parent(owner, *args, **kwargs)

    if not after_parents:
        monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_candidate_parents", count_parent)

    def expanded(owner, *args, **kwargs):
        original(owner, *args, **kwargs)
        if after_parents:
            calls.append(True)
        tenant = dict(owner.session.execute(sa.select(*Tenant.__table__.columns).limit(1)).one()._mapping)
        join = dict(owner.session.execute(sa.select(*Join.__table__.columns).limit(1)).one()._mapping)
        tid = str(uuid4())
        owner.session.execute(sa.insert(Tenant).values(**dict(tenant, id=tid)))
        owner.session.execute(sa.insert(Join).values(**dict(join, id=str(uuid4()), tenant_id=tid)))

    monkeypatch.setattr(CasdoorLoginScopeRepository, name, expanded)
    with pytest.raises(ValueError):
        reader(case).observe_invited_write_receipt(case.attempt)
    assert len(calls) == int(after_parents)
    assert current_state(case) == before


@pytest.mark.parametrize("kind", ["identity", "join", "intent", "history-text", "audit", "lifecycle", "issuance"])
def test_drift_after_first_locked_read_is_rejected_by_final_scope_or_exact_rows(writer_case, monkeypatch, kind):
    from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
    from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle

    case = writer_case()
    persist(case)
    before = current_state(case)
    original = CasdoorInvitedWriteReceiptRepository._rows
    calls = []

    def changed(owner, attempt, *, lock):
        rows = original(owner, attempt, lock=lock)
        calls.append(lock)
        if lock:
            model, values = {
                "identity": (Identity, {"sync_generation": 2}),
                "join": (Join, {"role": "owner"}),
                "intent": (Intent, {"error_code": "changed"}),
                "history-text": (History, {"desired_roles_json": "{}"}),
                "audit": (Audit, {"result_code": "changed"}),
                "lifecycle": (Lifecycle, {"epoch": 500}),
                "issuance": (Issuance, {"actor_id": str(uuid4())}),
            }[kind]
            owner.session.execute(sa.update(model).values(**values))
        return rows

    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "_rows", changed)
    with pytest.raises(ValueError):
        reader(case).observe_invited_write_receipt(case.attempt)
    assert calls[0] is True
    assert current_state(case) == before


@pytest.mark.parametrize("kind", ["autobegin", "explicit-used", "nested", "new", "dirty", "deleted"])
def test_reader_service_rejects_nonfresh_or_pending_session_before_owner_sql(writer_case, monkeypatch, kind):
    case = writer_case()
    persist(case)
    before = current_state(case)
    supplied = case.db()
    if kind == "autobegin":
        supplied.execute(sa.select(1))
    elif kind == "explicit-used":
        supplied.begin()
        supplied.execute(sa.select(1))
    elif kind == "nested":
        supplied.begin()
        supplied.begin_nested()
    elif kind == "new":
        supplied.add(Audit(action="unrelated"))
    elif kind == "dirty":
        supplied.get(Account, case.ids["account"]).name = "changed"
    else:
        supplied.delete(supplied.get(Account, case.ids["account"]))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reader owner reached for a nonfresh Session")

    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "observe", forbidden)
    with pytest.raises(ValueError):
        reader(case, factory=lambda: supplied).observe_invited_write_receipt(case.attempt)
    assert current_state(case) == before


def test_repository_rejects_previously_enlisted_explicit_root(writer_case):
    case = writer_case()
    persist(case)
    with case.db() as session, session.begin():
        session.execute(sa.select(1))
        with pytest.raises(ValueError):
            CasdoorInvitedWriteReceiptRepository(session, configuration_factory).observe(case.attempt)


@pytest.mark.parametrize("kind", ["dictionary", "object", "observation"])
def test_public_objects_cannot_select_or_authorize_reader(writer_case, kind):
    case = writer_case()
    persist(case)
    observation = reader(case).observe_invited_write_receipt(case.attempt)
    value = {
        "dictionary": {"operation_id": str(observation.operation_id)},
        "object": object(),
        "observation": observation,
    }[kind]

    def forbidden():
        raise AssertionError("wrong DTO reached session factory")

    with pytest.raises(ValueError):
        reader(case, factory=forbidden).observe_invited_write_receipt(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_generation", True),
        ("expected_generation", -1),
        ("expected_generation", 2**63),
        ("account_id", "not-a-uuid"),
        ("workspace_id", None),
        ("issuance_id", "bad"),
    ],
)
def test_malformed_exact_attempt_fails_before_sql(writer_case, field, value):
    case = writer_case()
    persist(case)
    bad = replace(case.attempt, **{field: value})
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ValueError):
            reader(case).observe_invited_write_receipt(bad)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture)
    assert statements == []


@pytest.mark.parametrize("model", [Audit, Intent, History])
def test_oversized_text_header_prevents_full_text_read(writer_case, model):
    case = writer_case()
    persist(case)
    column = {Audit: "summary_json", Intent: "desired_json", History: "desired_roles_json"}[model]
    with case.db() as session, session.begin():
        query = sa.update(model)
        if model is Audit:
            query = query.where(Audit.action == RECEIPT_KIND)
        session.execute(query.values({column: "é" * 17000}))
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ValueError):
            reader(case).observe_invited_write_receipt(case.attempt)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture)
    prefix = f"{model.__tablename__}.{column}"
    reads = [s for s in statements if s.startswith("SELECT") and prefix in s]
    assert reads
    assert all(f"length(CAST({prefix} AS BLOB))" in s for s in reads)
    assert not any(f", {prefix}," in s for s in reads)


def test_observation_is_not_accepted_as_writer_guard_or_prepared_login_scope(writer_case):
    from repositories.casdoor_invited_login_guard_repository_extend import _invitation_exclusion
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository

    case = writer_case()
    persist(case)
    observed = reader(case).observe_invited_write_receipt(case.attempt)
    before = current_state(case)
    with case.db() as session, session.begin():
        with pytest.raises(ValueError):
            _invitation_exclusion(observed, session, case.attempt.account_id, case.attempt.workspace_id)
        with pytest.raises(ValueError):
            CasdoorLoginScopeRepository(session, configuration_factory).discover(observed)
        with pytest.raises(ValueError):
            CasdoorAuditRepository(session)._append_invited_write_receipt(observed.receipt, invitation_guard=observed)
    assert current_state(case) == before
    assert not _REGISTRY


@pytest.mark.parametrize(
    "kind",
    ["before-created", "at-window-end", "after-window-end", "consumed-before", "consumed-after", "unknown", "pending"],
)
def test_completion_time_and_state_are_validated_independently_of_current_age(writer_case, monkeypatch, kind):
    from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
    from repositories import casdoor_invited_write_receipt_repository_extend as module

    case = writer_case(absent=True)
    persist(case)
    with case.db() as session, session.begin():
        operation = session.execute(sa.select(*Intent.__table__.columns)).one()
        monkeypatch.setattr(module, "_now", lambda: operation.created_at + timedelta(days=10))
        if kind in ("unknown", "pending"):
            session.execute(sa.update(Intent).values(operation_state=kind))
        elif kind.startswith("consumed-"):
            when = (
                operation.created_at - timedelta(seconds=1)
                if kind == "consumed-before"
                else operation.terminated_at + timedelta(seconds=1)
            )
            session.execute(sa.update(Issuance).values(consumed_at=when))
        else:
            when = {
                "before-created": operation.created_at - timedelta(seconds=1),
                "at-window-end": operation.created_at + timedelta(days=7),
                "after-window-end": operation.created_at + timedelta(days=8),
            }[kind]
            session.execute(sa.update(Intent).values(terminated_at=when, updated_at=when))
    assert_denied_without_writes(case)


@pytest.mark.parametrize("boundary", ["integration", "parents", "children", "final-read"])
def test_nowait_or_sql_error_returns_no_observation_and_no_writes(writer_case, monkeypatch, boundary):
    from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository

    case = writer_case()
    persist(case)
    before = current_state(case)

    def failure(*_args, **_kwargs):
        raise sa.exc.OperationalError("bounded SQL failed", {}, RuntimeError("denied"))

    if boundary == "integration":
        monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_integration", failure)
    elif boundary == "parents":
        monkeypatch.setattr(CasdoorLoginScopeRepository, "_lock_invited_candidate_parents", failure)
    else:
        original = CasdoorInvitedWriteReceiptRepository._rows

        def read(owner, attempt, *, lock):
            if boundary == "children" or not lock:
                return failure()
            return original(owner, attempt, lock=lock)

        monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "_rows", read)
    assert_denied_without_writes(case)
    assert current_state(case) == before


def test_read_root_close_failure_cannot_return_observation(writer_case):
    case = writer_case()
    persist(case)
    before = current_state(case)

    def factory():
        session = case.factory()
        rollback = session.rollback

        def failed_close():
            rollback()
            raise RuntimeError("read root rollback failed")

        session.rollback = failed_close
        return session

    with pytest.raises(RuntimeError, match="read root rollback failed"):
        reader(case, factory=factory).observe_invited_write_receipt(case.attempt)
    assert current_state(case) == before


@pytest.mark.parametrize("bad", [True, None, {}, 0])
def test_reader_rejects_non_attempt_before_session_creation(bad):
    def forbidden():
        raise AssertionError("Session created for invalid input")

    with pytest.raises(ValueError):
        CasdoorInvitedWriteReceiptService(
            session_factory=forbidden, configuration_factory=forbidden
        ).observe_invited_write_receipt(bad)
