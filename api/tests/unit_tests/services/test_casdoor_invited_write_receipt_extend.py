"""Real P3L/B3 receipt producer, bounded parser and rollback; offline SQLite only."""

import hashlib
import json
from dataclasses import FrozenInstanceError
from enum import StrEnum
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MEMBERSHIP_FIELDS,
    POSTWRITE_DOMAIN,
    RECEIPT_KIND,
    InvitedLocalWriteReceipt,
)
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invited_login_guard_repository_extend import _REGISTRY
from services.casdoor_invited_local_membership_service_extend import CasdoorInvitedLocalMembershipService
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


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def audits(case):
    with case.db() as session:
        return tuple(
            session.execute(
                sa.select(*Audit.__table__.columns)
                .where(
                    Audit.action == RECEIPT_KIND,
                    Audit.correlation_id == str(case.pending.snapshot.operation_id),
                )
                .order_by(Audit.id)
            )
        )


def result_values(item):
    return {
        "workspace_id": str(item.workspace_id),
        "ownership_decision": item.ownership_decision.value,
        "intent_barrier": item.intent_barrier.value,
        "outcome": item.outcome.value,
        "join_id": str(item.join_id) if item.join_id is not None else None,
        "membership_id": str(item.membership_id) if item.membership_id is not None else None,
        "current_role": item.current_role.value if item.current_role is not None else None,
        "membership_created": item.membership_created,
        "role_changed": item.role_changed,
        "metadata_changed": item.metadata_changed,
        "membership_regranted": item.membership_regranted,
    }


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("invite_target", [False, True])
def test_actual_service_commits_one_exact_receipt_without_synthetic_results(writer_case, absent, invite_target):
    case = writer_case(absent=absent, invite_target=invite_target)
    before = database(case)
    assert audits(case) == ()
    result = persist(case)
    rows = audits(case)
    assert len(rows) == 1
    row = rows[0]
    receipt = InvitedLocalWriteReceipt(row.summary_json)
    data = receipt.values()
    assert row.action == RECEIPT_KIND
    assert row.result_code == "verified"
    assert row.actor_account_id is None
    assert row.correlation_id == str(case.pending.snapshot.operation_id)
    assert data["references"] == {
        "operation_id": row.correlation_id,
        "issuance_id": case.ids["issuance"],
        "integration_id": case.ids["integration"],
        "namespace_id": row.namespace_id,
        "revision_id": row.revision_id,
        "identity_id": row.identity_id,
        "account_id": row.account_id,
        "workspace_id": case.ids["workspace"],
        "invitation_join_id": case.completed.facts.join_id,
    }
    assert (row.namespace_id, row.revision_id, row.account_id, row.identity_id) == (
        str(result.namespace_id),
        str(result.revision_id),
        str(result.account_id),
        str(result.identity_id),
    )
    assert data["results"] == [result_values(item) for item in result.workspaces]
    assert (case.ids["workspace"] in {item["workspace_id"] for item in data["results"]}) is invite_target
    assert data["completion_proof_ref"] == case.completed.facts.proof_ref
    assert data["scope_digest"] == case.pending.snapshot.scope_digest
    assert data["payload_digest"] == before["invitation_authority_issuance_extend"][0].payload_digest
    assert (data["generation_before"], data["generation_after"], data["fence_epoch"]) == (
        0,
        1,
        case.context.fence_epoch,
    )
    with case.db() as session:

        def fields(model, names, order):
            return [
                {key: value.value if isinstance(value, StrEnum) else value for key, value in row._mapping.items()}
                for row in session.execute(sa.select(*(getattr(model, name) for name in names)).order_by(*order))
            ]

        projection = {
            "identity": fields(Identity, IDENTITY_FIELDS, (Identity.id,))[0],
            "joins": fields(Join, JOIN_FIELDS, (Join.tenant_id, Join.id)),
            "memberships": fields(History, MEMBERSHIP_FIELDS, (History.namespace_id, History.workspace_id, History.id)),
        }
    envelope = {"domain": POSTWRITE_DOMAIN, "operation_id": row.correlation_id, "postwrite": projection}
    assert data["postwrite_sha256"] == hashlib.sha256(canonical(envelope).encode()).hexdigest()
    assert row.summary_json == canonical(data)
    assert len(row.summary_json.encode()) <= 32768
    for private in ("ExactSubject", "invitee@example.test", "payload_json", "password", "desired_json"):
        assert private not in row.summary_json
        assert private not in canonical(projection)
    assert not _REGISTRY
    with pytest.raises(FrozenInstanceError):
        receipt.canonical_json = "changed"
    data["references"]["account_id"] = str(uuid4())
    assert receipt.values()["references"]["account_id"] == row.account_id


@pytest.mark.parametrize("after_flush", [False, True])
def test_audit_failure_after_real_b3_rolls_back_everything(writer_case, monkeypatch, after_flush):
    case = writer_case()
    before = database(case)
    original = CasdoorAuditRepository._append_invited_write_receipt
    calls = []

    def fail(owner, receipt, **kwargs):
        calls.append(True)
        assert owner._session.scalar(sa.select(Identity.sync_generation)) == 1
        assert owner._session.scalar(sa.select(sa.func.count()).select_from(History)) == 2
        if after_flush:
            original(owner, receipt, **kwargs)
            assert (
                owner._session.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == RECEIPT_KIND))
                == 1
            )
        raise RuntimeError("injected audit append failure")

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", fail)
    with pytest.raises(RuntimeError, match="injected audit append failure"):
        persist(case)
    assert calls == [True]
    assert database(case) == before
    assert audits(case) == ()
    assert not _REGISTRY


def test_unknown_commit_does_not_retry_or_claim_success(writer_case, monkeypatch):
    case = writer_case()
    commits, appends = [], []
    original = CasdoorAuditRepository._append_invited_write_receipt

    def appended(owner, *args, **kwargs):
        appends.append(True)
        return original(owner, *args, **kwargs)

    def unknown(session):
        commits.append(True)
        raise RuntimeError("unknown receipt commit")

    def factory():
        session = case.factory()
        sa.event.listen(session, "after_commit", unknown)
        return session

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", appended)
    case.writer = CasdoorInvitedLocalMembershipService(
        session_factory=factory, configuration_factory=configuration_factory
    )
    with pytest.raises(RuntimeError, match="unknown receipt commit"):
        persist(case)
    assert appends == [True]
    assert commits == [True]
    assert len(audits(case)) == 1
    assert not _REGISTRY


def schema_value():
    """Synthetic parser fixture only: it cannot authorize an audit append."""
    refs = dict(
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
    )
    return {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "references": refs,
        "generation_before": 4,
        "generation_after": 5,
        "fence_epoch": 0,
        "scope_digest": "0" * 64,
        "payload_digest": "1" * 64,
        "postwrite_sha256": "2" * 64,
        "completion_proof_ref": f"v1:{refs['invitation_join_id']}:0:1:" + "3" * 64,
        "results": [
            {
                "workspace_id": refs["workspace_id"],
                "ownership_decision": "preserve_unmanaged",
                "intent_barrier": "clear",
                "outcome": "preserved",
                "join_id": refs["invitation_join_id"],
                "membership_id": None,
                "current_role": "normal",
                "membership_created": False,
                "role_changed": False,
                "metadata_changed": False,
                "membership_regranted": False,
            }
        ],
    }


@pytest.mark.parametrize("field", ["schema_version", "generation_before", "generation_after", "fence_epoch"])
@pytest.mark.parametrize("value", [True, False, None, "1", 1.0, -1, 2**63])
def test_schema_rejects_wrong_counter_types_and_bounds(field, value):
    data = schema_value()
    data[field] = value
    with pytest.raises(ValueError, match="invited_write_receipt_invalid"):
        InvitedLocalWriteReceipt.from_values(data)


def test_counter_maximum_valid_and_receipt_has_no_mutable_internals():
    data = schema_value()
    data.update(generation_before=2**63 - 2, generation_after=2**63 - 1, fence_epoch=2**63 - 1)
    receipt = InvitedLocalWriteReceipt.from_values(data)
    assert receipt.values() == data
    data["results"][0]["current_role"] = "admin"
    assert receipt.values()["results"][0]["current_role"] == "normal"


@pytest.mark.parametrize("location", ["top", "references", "result"])
@pytest.mark.parametrize("kind", ["unknown", "missing", "duplicate"])
def test_closed_objects_reject_extra_missing_and_duplicate_keys(location, kind):
    data = schema_value()
    target = data if location == "top" else data["references"] if location == "references" else data["results"][0]
    key = next(iter(target))
    if kind == "unknown":
        target["private_payload"] = "forbidden"
    elif kind == "missing":
        del target[key]
    raw = canonical(data)
    if kind == "duplicate":
        token = json.dumps(key) + ":" + canonical(target[key])
        raw = raw.replace(token, token + "," + token, 1)
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt(raw)


@pytest.mark.parametrize("field", list(schema_value()["references"]))
@pytest.mark.parametrize("bad", ["", None, True, "123", "ABCDEF01-ABCD-ABCD-ABCD-ABCDEF012345"])
def test_references_require_canonical_uuid(field, bad):
    data = schema_value()
    data["references"][field] = bad
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt.from_values(data)


@pytest.mark.parametrize("field", ["scope_digest", "payload_digest", "postwrite_sha256"])
@pytest.mark.parametrize("bad", [None, 0, "F" * 64, "0" * 63, "0" * 65])
def test_digests_are_exact_lowercase_sha256(field, bad):
    data = schema_value()
    data[field] = bad
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt.from_values(data)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "é",
        "a" * 129,
        "v2:bad:0:1:" + "0" * 64,
        "v1:00000000-0000-0000-0000-000000000009:0:0:" + "0" * 64,
        "v1:00000000-0000-0000-0000-000000000009:0:9007199254740992:" + "0" * 64,
        "v1:00000000-0000-0000-0000-000000000008:0:1:" + "0" * 64,
    ],
)
def test_completion_proof_is_bounded_shaped_and_bound_to_invitation_join(bad):
    data = schema_value()
    data["completion_proof_ref"] = bad
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt.from_values(data)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("workspace_id", None),
        ("join_id", None),
        ("membership_id", "bad"),
        ("ownership_decision", "invented"),
        ("intent_barrier", "pending"),
        ("outcome", "pending"),
        ("current_role", None),
        ("current_role", "invented"),
        ("membership_created", 1),
        ("role_changed", 0),
        ("metadata_changed", None),
        ("membership_regranted", True),
        ("join_id", "00000000-0000-0000-0000-000000000010"),
    ],
)
def test_result_fields_have_closed_types_and_observed_only_outcomes(field, bad):
    data = schema_value()
    data["results"][0][field] = bad
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt.from_values(data)


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "duplicate",
        "unsorted",
        "over-count",
        "generation-delta",
        "noncanonical",
        "nan",
        "oversize",
        "surrogate",
    ],
)
def test_parser_rejects_invalid_collections_and_encoding(kind):
    data = schema_value()
    if kind == "empty":
        data["results"] = []
    elif kind == "duplicate":
        data["results"] *= 2
    elif kind == "unsorted":
        data["results"] = [dict(data["results"][0], workspace_id=str(UUID(int=i))) for i in [100, 99]]
    elif kind == "over-count":
        data["results"] = [dict(data["results"][0], workspace_id=str(UUID(int=i))) for i in range(100, 201)]
    elif kind == "generation-delta":
        data["generation_after"] = 7
    raw = canonical(data)
    if kind == "noncanonical":
        raw = json.dumps(data)
    elif kind == "nan":
        raw = raw.replace('"fence_epoch":0', '"fence_epoch":NaN')
    elif kind == "oversize":
        raw = " " * 32769
    elif kind == "surrogate":
        raw = "\ud800"
    with pytest.raises(ValueError):
        InvitedLocalWriteReceipt(raw)


def all_audits(case):
    with case.db() as session:
        return tuple(session.execute(sa.select(*Audit.__table__.columns).order_by(Audit.id)))


@pytest.mark.parametrize("column", list(Audit.__table__.columns.keys()) + ["delete", "duplicate", "oversized"])
def test_every_audit_column_drift_after_flush_rolls_back_root(writer_case, monkeypatch, column):
    from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import changed_value

    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorAuditRepository._append_invited_write_receipt
    calls = []

    def altered(owner, receipt, **kwargs):
        row = original(owner, receipt, **kwargs)
        calls.append(True)
        if column == "delete":
            owner._session.execute(sa.delete(Audit).where(Audit.id == row.id))
        elif column == "duplicate":
            owner._session.execute(sa.insert(Audit).values(**dict(row._mapping, id=str(uuid4()))))
        else:
            values = (
                {"summary_json": "a" * 32769}
                if column == "oversized"
                else {
                    column: changed_value(Audit.__table__.c[column], getattr(row, column)),
                }
            )
            owner._session.execute(sa.update(Audit).where(Audit.id == row.id).values(**values))
        return row

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", altered)
    with pytest.raises(ValueError):
        persist(case)
    assert calls == [True]
    assert database(case) == before
    assert all_audits(case) == old_audits
    assert not _REGISTRY


@pytest.mark.parametrize(
    ("position", "kind"),
    [
        (p, k)
        for p in [7, 8]
        for k in ["audit", "identity", "join", "history-text", "integration", "intent"]
        if (p, k) != (7, "audit")
    ],
)
def test_drift_after_receipt_lease_io_rejects_before_commit(writer_case, monkeypatch, position, kind):
    from models.casdoor_extend import CasdoorIntegrationExtend as Integration
    from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository

    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorInvitedLoginGuardRepository._leases
    calls = []

    def altered(owner, value):
        original(owner, value)
        calls.append(True)
        if len(calls) == position:
            model, updates = {
                "audit": (Audit, {"result_code": "changed"}),
                "identity": (Identity, {"sync_generation": 5}),
                "join": (Join, {"role": "admin"}),
                "history-text": (History, {"desired_roles_json": "{}"}),
                "integration": (Integration, {"etag": 500}),
                "intent": (Intent, {"desired_json": "{}"}),
            }[kind]
            owner.session.execute(sa.update(model).values(**updates))

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "_leases", altered)
    with pytest.raises(ValueError):
        persist(case)
    assert database(case) == before
    assert all_audits(case) == old_audits
    assert len(calls) >= position
    assert not _REGISTRY


@pytest.mark.parametrize("kind", ["dict", "parsed-copy", "wrong-session", "revoked", "repeat"])
def test_receipt_shape_never_grants_append_authority(writer_case, monkeypatch, kind):
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository

    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorAuditRepository._append_invited_write_receipt
    calls = []

    def attempted(owner, receipt, **kwargs):
        calls.append(True)
        if kind == "dict":
            return original(owner, receipt.values(), **kwargs)
        if kind == "parsed-copy":
            return original(owner, InvitedLocalWriteReceipt(receipt.canonical_json), **kwargs)
        if kind == "wrong-session":
            with case.db() as other, other.begin():
                return original(CasdoorAuditRepository(other), receipt, **kwargs)
        if kind == "revoked":
            CasdoorInvitedLoginGuardRepository.revoke(kwargs["invitation_guard"])
        if kind == "repeat":
            original(owner, receipt, **kwargs)
        return original(owner, receipt, **kwargs)

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", attempted)
    with pytest.raises(ValueError):
        persist(case)
    assert calls == [True]
    assert database(case) == before
    assert all_audits(case) == old_audits
    assert not _REGISTRY


@pytest.mark.parametrize("kind", ["new", "dirty", "deleted", "nested", "ended-root", "duplicate-row"])
def test_pending_state_wrong_root_and_preexisting_exact_receipt_are_rejected(writer_case, monkeypatch, kind):
    from models.account import Account

    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorAuditRepository._append_invited_write_receipt
    calls = []

    def attempted(owner, receipt, **kwargs):
        calls.append(True)
        session = owner._session
        if kind == "new":
            session.add(Audit(action="unrelated"))
        elif kind == "dirty":
            session.get(Account, case.ids["account"]).name = "unrelated"
        elif kind == "deleted":
            session.delete(session.get(Account, case.ids["account"]))
        elif kind == "nested":
            with session.begin_nested():
                return original(owner, receipt, **kwargs)
        elif kind == "ended-root":
            session.rollback()
            return original(owner, receipt, **kwargs)
        else:
            refs = receipt.values()["references"]
            session.execute(
                sa.insert(Audit).values(
                    id=str(uuid4()),
                    action=RECEIPT_KIND,
                    result_code="verified",
                    namespace_id=refs["namespace_id"],
                    revision_id=refs["revision_id"],
                    identity_id=refs["identity_id"],
                    account_id=refs["account_id"],
                    correlation_id=refs["operation_id"],
                    summary_json=receipt.canonical_json,
                )
            )
        statements = []

        def observed(*_args):
            statements.append(True)

        sa.event.listen(case.engine, "before_cursor_execute", observed)
        try:
            return original(owner, receipt, **kwargs)
        finally:
            sa.event.remove(case.engine, "before_cursor_execute", observed)
            if kind in ("new", "dirty", "deleted"):
                assert statements == []

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", attempted)
    with pytest.raises((ValueError, RuntimeError)):
        persist(case)
    assert calls == [True]
    assert database(case) == before
    assert all_audits(case) == old_audits
    assert not _REGISTRY


@pytest.mark.parametrize("position", [7, 8])
@pytest.mark.parametrize("kind", ["lease-error", "deadline"])
def test_receipt_lease_and_deadline_failure_roll_back_whole_root(writer_case, monkeypatch, position, kind):
    from core.casdoor.leases import CasdoorLeaseError
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository

    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorInvitedLoginGuardRepository._leases
    calls = []

    def altered(owner, value):
        original(owner, value)
        calls.append(True)
        if len(calls) == position:
            if kind == "lease-error":
                raise CasdoorLeaseError("injected lease failure")
            value.deadline = 0

    monkeypatch.setattr(CasdoorInvitedLoginGuardRepository, "_leases", altered)
    with pytest.raises((ValueError, RuntimeError, CasdoorLeaseError)):
        persist(case)
    assert len(calls) == position
    assert database(case) == before
    assert all_audits(case) == old_audits
    assert not _REGISTRY


def privacy_value():
    refs = schema_value()["references"]
    return {
        "identity": {
            "id": refs["identity_id"],
            "namespace_id": refs["namespace_id"],
            "account_id": refs["account_id"],
            "sync_generation": 1,
        },
        "joins": (
            {
                "id": refs["invitation_join_id"],
                "account_id": refs["account_id"],
                "tenant_id": refs["workspace_id"],
                "role": "normal",
            },
        ),
        "memberships": (),
    }


def test_privacy_projection_sorts_and_binds_operation_with_fixed_domain():
    from core.casdoor.invited_write_receipt import postwrite_projection, postwrite_sha256

    value = privacy_value()
    joins = value["joins"]
    value["joins"] = (dict(joins[0], id=str(UUID(int=100)), tenant_id=str(UUID(int=101))), *joins)
    projection = postwrite_projection(**value)
    assert projection["joins"][0] == joins[0]
    digest = postwrite_sha256(str(UUID(int=1)), **value)
    value["joins"] = tuple(reversed(value["joins"]))
    assert digest == postwrite_sha256(str(UUID(int=1)), **value)
    assert digest != postwrite_sha256(str(UUID(int=2)), **value)
    expected = {"domain": POSTWRITE_DOMAIN, "operation_id": str(UUID(int=1)), "postwrite": projection}
    assert digest == hashlib.sha256(canonical(expected).encode()).hexdigest()


@pytest.mark.parametrize(
    "kind", ["identity-pii", "join-pii", "wrong-account", "duplicate", "over-cap", "bool-generation", "list", "role"]
)
def test_privacy_projection_rejects_nonwhitelisted_or_unbounded_data(kind):
    from core.casdoor.invited_write_receipt import postwrite_projection

    value = privacy_value()
    if kind == "identity-pii":
        value["identity"]["subject"] = "secret"
    elif kind == "join-pii":
        value["joins"][0]["email"] = "secret"
    elif kind == "wrong-account":
        value["joins"][0]["account_id"] = str(UUID(int=100))
    elif kind == "duplicate":
        value["joins"] *= 2
    elif kind == "over-cap":
        value["joins"] *= 2049
    elif kind == "bool-generation":
        value["identity"]["sync_generation"] = True
    elif kind == "list":
        value["joins"] = list(value["joins"])
    else:
        value["joins"][0]["role"] = "invented"
    with pytest.raises(ValueError):
        postwrite_projection(**value)


def test_oversized_audit_stops_at_length_header_before_summary_materialization(writer_case, monkeypatch):
    case = writer_case()
    before, old_audits = database(case), all_audits(case)
    original = CasdoorAuditRepository._append_invited_write_receipt
    statements = []

    def observed(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def altered(owner, receipt, **kwargs):
        row = original(owner, receipt, **kwargs)
        owner._session.execute(sa.update(Audit).where(Audit.id == row.id).values(summary_json="é" * 16385))
        sa.event.listen(case.engine, "before_cursor_execute", observed)
        return row

    monkeypatch.setattr(CasdoorAuditRepository, "_append_invited_write_receipt", altered)
    try:
        with pytest.raises(ValueError):
            persist(case)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", observed)
    reads = [s for s in statements if s.startswith("SELECT") and "FROM casdoor_audit_extend" in s]
    assert len(reads) == 1
    assert "length(CAST(casdoor_audit_extend.summary_json AS BLOB))" in reads[0]
    assert "casdoor_audit_extend.created_at" not in reads[0]
    assert database(case) == before
    assert all_audits(case) == old_audits
