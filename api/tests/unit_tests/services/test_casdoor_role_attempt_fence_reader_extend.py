"""Actual SQLite observation only; synthetic commits cannot prove remote safety."""

import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.rbac_replace import RbacReplaceError, replace_rbac
from models.account import Account, TenantStatus
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_fence_reader_repository_extend import (
    CasdoorRoleAttemptFenceReaderRepository,
)
from repositories.casdoor_role_attempt_repository_extend import (
    CasdoorRoleAttemptRepository,
)
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict
from services.casdoor_role_attempt_fence_reader_service_extend import (
    CasdoorRoleAttemptFenceReaderService,
)
from sqlalchemy.orm import Session

from tests.unit_tests.repositories import (
    test_casdoor_role_attempt_dispatch_fence_extend as fence,
)

storage = fence.storage
staged = fence.staged
reserved = fence.reserved


@pytest.fixture
def armed(reserved):
    with reserved.session.begin():
        fence.arm(reserved)
    return reserved


class TrackedSession(Session):
    """Lifecycle capture without substituting any repository read or DB policy."""

    def __init__(self, bind, events, failure=None):
        super().__init__(bind, expire_on_commit=False)
        self.events = events
        self.failure = failure
        for name in ("before_flush", "before_commit"):
            sa.event.listen(self, name, lambda *_args, event=name: self.events.append(event))

    def begin(self, *args, **kwargs):
        self.events.append("begin")
        return super().begin(*args, **kwargs)

    def rollback(self):
        self.events.append("rollback")
        if self.failure == "rollback_before":
            raise RuntimeError("fixture rollback failure before cleanup")
        super().rollback()
        if self.failure == "rollback":
            raise RuntimeError("fixture rollback failure")

    def close(self):
        self.events.append("close")
        super().close()
        if self.failure == "close":
            raise RuntimeError("fixture close failure")


def reader(s, *, failure=None, factory=None):
    events, sessions = [], []

    def fresh():
        session = TrackedSession(s.engine, events, failure)
        sessions.append(session)
        return session

    service = CasdoorRoleAttemptFenceReaderService(session_factory=factory or fresh)
    return service, events, sessions


def observe(s, service=None, **kwargs):
    if service is None:
        service = reader(s)[0]
    return service.observe_dispatch_fence(
        kwargs.get("version", s.version),
        kwargs.get("target", s.target),
        intent_id=kwargs.get("intent_id", s.receipt.intent_id),
        reservation_id=kwargs.get("reservation_id", s.reservation_id),
    )


def unchanged_refusal(s, **kwargs):
    with s.session.begin():
        before = fence.reservation.snapshot(s)
    statements = fence.reservation.trace(s)
    service, events, sessions = reader(s)
    with pytest.raises(CasdoorRoleIntentConflict) as error:
        observe(s, service, **kwargs)
    assert str(error.value) == "authorization_pending"
    assert not fence.reservation.dml(statements)
    assert events[-2:] == ["rollback", "close"]
    assert "before_flush" not in events and "before_commit" not in events
    assert sessions and not sessions[0].in_transaction()
    with s.session.begin():
        assert fence.reservation.snapshot(s) == before


def test_actual_commit_lost_ack_fresh_observation_escapes_after_rollback_and_close(reserved):
    s = reserved
    s.session.begin()
    fence.arm(s)
    original = s.session.commit

    def lost_ack():
        original()
        raise RuntimeError("fixture lost acknowledgement")

    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        lost_ack()
    with s.session.begin():
        before = fence.reservation.snapshot(s)
    statements = fence.reservation.trace(s)
    service, events, sessions = reader(s)
    result = observe(s, service)
    assert events == ["begin", "rollback", "close"]
    assert sessions[0] is not s.session and not sessions[0].in_transaction()
    assert result.intent_id == s.receipt.intent_id and result.reservation_id == s.reservation_id
    assert result.membership_id == s.receipt.membership_id and result.join_id == s.receipt.join_id
    assert result.scope_digest == s.receipt.scope_digest
    assert result.desired_payload_digest == s.receipt.desired_payload_digest
    assert {field.name for field in fields(result)} == {
        "intent_id",
        "reservation_id",
        "membership_id",
        "join_id",
        "desired_payload_digest",
        "scope_digest",
    }
    assert str(result.intent_id) not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.reservation_id = uuid4()
    with pytest.raises(RbacReplaceError):
        replace_rbac(capability=result)
    assert observe(s) == result
    assert not fence.reservation.dml(statements)
    assert not any("SAVEPOINT" in stmt.upper() for stmt in statements)
    with s.session.begin():
        assert fence.reservation.snapshot(s) == before


@pytest.mark.parametrize("state", ["unreserved", "reserved", "missing", "wrong_id", "wrong_attempt"])
def test_absence_and_unarmed_state_never_imply_retry(staged, state):
    s = staged
    if state != "unreserved":
        with s.session.begin():
            fence.reservation.reserve(s)
            if state == "missing":
                s.session.execute(sa.delete(Intent))
            elif state in ("wrong_id", "wrong_attempt"):
                fence.arm(s)
    kwargs = {"intent_id": uuid4()} if state == "wrong_id" else {}
    if state == "wrong_attempt":
        kwargs["reservation_id"] = uuid4()
    unchanged_refusal(s, **kwargs)


_EFFECTS = [
    (name, datetime(2000, 1, 1))
    for name in ("lease_expires_at", "sent_at", "acknowledged_at", "readback_at", "terminated_at", "retry_at")
]
_EFFECTS += [(name, "untrusted") for name in ("lease_owner", "termination_proof_kind", "proof_ref", "error_code")]
_EFFECTS += [
    ("attempt_count", 0),
    ("attempt_count", 2),
    ("attempt_id", None),
    ("operation_state", CasdoorOperationState.PENDING),
    ("operation_state", CasdoorOperationState.UNKNOWN),
    ("operation_state", CasdoorOperationState.APPLIED),
    ("operation_state", CasdoorOperationState.FAILED),
    ("operation_state", CasdoorOperationState.CANCELLED),
    ("termination_state", CasdoorTerminationState.NOT_STARTED),
    ("termination_state", CasdoorTerminationState.CONFIRMED),
    ("termination_state", CasdoorTerminationState.MANUAL_RECOVERY),
    ("kind", CasdoorIntentKind.PROFILE_AVATAR),
    ("resource_type", "app"),
    ("resource_id", "00000000-0000-0000-0000-000000000099"),
    ("desired_json", "{}"),
    ("scope_digest", "f" * 64),
    ("idempotency_key", "e" * 64),
    ("generation", 2),
    ("fence_epoch", 1),
    ("ownership_epoch", 8),
]


@pytest.mark.parametrize("field,value", _EFFECTS, ids=[f"{name}-{i}" for i, (name, _) in enumerate(_EFFECTS)])
def test_every_populated_effect_or_projection_drift_refuses(armed, field, value):
    with armed.session.begin():
        armed.session.execute(sa.update(Intent).values({field: value}))
    unchanged_refusal(armed)


_CHAIN = fence._CHAIN + [
    ("workspace", "status", TenantStatus.ARCHIVE),
    ("namespace", "lifecycle", CasdoorNamespaceLifecycle.FENCING),
    ("namespace", "lifecycle", CasdoorNamespaceLifecycle.ARCHIVED),
    ("namespace", "expected_issuer", "https://changed.example.test"),
    ("namespace", "organization", "Changed"),
    ("namespace", "application", "Changed"),
    ("namespace", "client_id", "Changed"),
    ("revision", "policy_json", "{}"),
    ("identity", "issuer", "https://changed.example.test"),
    ("identity", "organization", "Changed"),
    ("identity", "subject_digest", "f" * 64),
    ("history", "last_applied_roles_json", "{}"),
]


@pytest.mark.parametrize(
    "model,field,value", _CHAIN, ids=[f"{model}-{field}-{i}" for i, (model, field, _) in enumerate(_CHAIN)]
)
def test_complete_current_owner_chain_drift_refuses(armed, model, field, value):
    obj = getattr(armed, model)
    with armed.session.begin():
        armed.session.execute(sa.update(type(obj)).where(type(obj).id == obj.id).values({field: value}))
    unchanged_refusal(armed)


@pytest.mark.parametrize("kind", list(CasdoorIntentKind), ids=lambda kind: kind.value)
@pytest.mark.parametrize("association", ["scope", "workspace_null", "membership"])
def test_foreign_related_intents_fail_closed_except_unrelated_avatar(armed, kind, association):
    s = armed
    with s.session.begin():
        values = fence.row(s)
        values.update(
            id=str(uuid4()),
            identity_id=str(uuid4()),
            generation=2,
            scope_digest="f" * 64,
            idempotency_key="e" * 64,
            kind=kind,
            operation_state=CasdoorOperationState.UNKNOWN,
            account_id=str(uuid4()) if association == "membership" else s.account.id,
            workspace_id=s.workspace.id if association == "scope" else None,
            membership_id=s.history.id if association == "membership" else None,
        )
        s.session.execute(sa.insert(Intent).values(**values))
    if kind is CasdoorIntentKind.PROFILE_AVATAR:
        statements = fence.reservation.trace(s)
        observe(s)
        assert not fence.reservation.dml(statements)
    else:
        unchanged_refusal(s)


@pytest.mark.parametrize("failure", ["read", "rollback", "rollback_before", "close", "factory"])
def test_lifecycle_failure_never_returns_partial_observation(armed, monkeypatch, failure):
    service, events, _sessions = reader(armed, failure=failure)

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    if failure == "read":
        monkeypatch.setattr(CasdoorRoleAttemptRepository, "_read_attempt_scope", fail)
    elif failure == "factory":
        service = reader(armed, factory=fail)[0]
    statements = fence.reservation.trace(armed)
    with pytest.raises(CasdoorRoleIntentConflict):
        observe(armed, service)
    if failure != "factory":
        assert events[-2:] == ["rollback", "close"]
    assert not fence.reservation.dml(statements)


@pytest.mark.parametrize("invalid", ["begun", "enlisted", "new", "dirty", "deleted", "nested", "inactive", "bound"])
def test_service_root_preflight_refuses_before_repository_sql(armed, monkeypatch, invalid):
    s = armed
    events = []
    session = TrackedSession(s.engine, events)
    connection = None
    if invalid == "bound":
        connection = s.engine.connect()
        connection.begin()
        session.close()
        session = TrackedSession(connection, events)
    elif invalid == "inactive":
        monkeypatch.setattr(TrackedSession, "is_active", property(lambda _s: False))
    elif invalid == "new":
        session.add(Account(name="Pending", email="pending@example.test"))
    elif invalid in ("dirty", "deleted"):
        obj = session.get(Account, s.account.id)
        if invalid == "dirty":
            obj.name = "Pending"
        else:
            session.delete(obj)
    else:
        session.begin()
        if invalid == "enlisted":
            session.connection()
        elif invalid == "nested":
            session.begin_nested()
    statements = fence.reservation.trace(s)
    try:
        with pytest.raises(CasdoorRoleIntentConflict):
            observe(s, reader(s, factory=lambda: session)[0])
        assert not statements
        assert events[-2:] == ["rollback", "close"]
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


@pytest.mark.parametrize("invalid", ["none", "autobegin", "enlisted", "nested", "dirty", "new", "rbac_off"])
def test_repository_requires_clean_explicit_unenlisted_root(armed, monkeypatch, invalid):
    with Session(armed.engine, expire_on_commit=False) as session:
        if invalid == "autobegin":
            session.get(Account, armed.account.id)
        elif invalid != "none":
            session.begin()
        if invalid == "enlisted":
            session.connection()
        elif invalid == "nested":
            session.begin_nested()
        elif invalid == "dirty":
            obj = session.get(Account, armed.account.id)
            obj.name = "Pending"
        elif invalid == "new":
            session.add(Account(name="Pending", email="pending@example.test"))
        elif invalid == "rbac_off":
            monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
        statements = fence.reservation.trace(armed)
        with pytest.raises((CasdoorRoleIntentConflict, RuntimeError)):
            CasdoorRoleAttemptFenceReaderRepository(session).observe(
                armed.version, armed.target, intent_id=armed.receipt.intent_id, reservation_id=armed.reservation_id
            )
        assert not statements
        session.rollback()


@pytest.mark.parametrize("field", ["intent_id", "reservation_id"])
@pytest.mark.parametrize("value_kind", ["string", "bool", "subclass", "none"])
def test_exact_uuid_inputs_refuse_before_factory(armed, field, value_kind):
    class OtherUUID(UUID):
        pass

    service, events, sessions = reader(armed)
    value = {"string": str(armed.reservation_id), "bool": True, "subclass": OtherUUID(str(uuid4())), "none": None}
    with pytest.raises(CasdoorRoleIntentConflict):
        observe(armed, service, **{field: value[value_kind]})
    assert not events and not sessions


@pytest.mark.parametrize(
    "invalid", ["generation_bool", "generation_overflow", "fence_bool", "fence_overflow", "target_owner"]
)
def test_existing_validation_owner_refuses_invalid_plan(armed, invalid):
    version, target = armed.version, armed.target
    if invalid.startswith("generation"):
        version = replace(version, generation=True if invalid.endswith("bool") else 2**63)
    elif invalid.startswith("fence"):
        version = replace(version, fence_epoch=False if invalid.endswith("bool") else 2**63)
    else:
        target = replace(target, target_role="owner")
        version = replace(version, plan=replace(version.plan, targets=(target,)))
    unchanged_refusal(armed, version=version, target=target)


@pytest.mark.parametrize("part", ["scope", "digest", "values", "join", "history", "row", "timestamp", "exception"])
def test_second_full_read_drift_never_escapes(armed, monkeypatch, part):
    original = CasdoorRoleAttemptRepository._read_attempt_scope
    count = 0

    def drift(owner, *args):
        nonlocal count
        count += 1
        result = list(original(owner, *args))
        if count == 2:
            if part == "exception":
                raise RuntimeError("fixture SQL read failure")
            index = {"scope": 0, "digest": 1, "values": 2, "join": 3, "history": 4, "row": 5, "timestamp": 5}[part]
            if part in ("scope", "values", "row", "timestamp"):
                result[index] = dict(result[index])
                result[index]["created_at" if part == "timestamp" else "drift"] = "changed"
            else:
                result[index] = "changed"
        return tuple(result)

    monkeypatch.setattr(CasdoorRoleAttemptRepository, "_read_attempt_scope", drift)
    unchanged_refusal(armed)
    assert count == 2


@pytest.mark.parametrize("interference", ["parent", "effect", "timestamp", "related"])
def test_actual_sql_read_time_change_refuses_and_cleanup_restores_root(armed, monkeypatch, interference):
    s = armed
    with s.session.begin():
        before = fence.reservation.snapshot(s)
    original = CasdoorRoleAttemptRepository._read_attempt_scope
    calls = 0

    def interfere(owner, *args):
        nonlocal calls
        calls += 1
        result = original(owner, *args)
        if calls == 1:
            if interference == "parent":
                statement = sa.update(fence.reservation.staging.Identity).values(sync_generation=2)
            elif interference == "related":
                values = dict(result[-1])
                values.update(id=str(uuid4()), scope_digest="f" * 64, idempotency_key="e" * 64)
                statement = sa.insert(Intent).values(**values)
            else:
                values = (
                    {"proof_ref": "fixture interference"}
                    if interference == "effect"
                    else {"created_at": datetime(2000, 1, 1)}
                )
                statement = sa.update(Intent).values(**values)
            # Deliberate fixture interference in the reader's root, never reader
            # code DML. This gives real scalar rereads changed SQL evidence.
            owner._session.execute(statement)
        return result

    monkeypatch.setattr(CasdoorRoleAttemptRepository, "_read_attempt_scope", interfere)
    service, events, sessions = reader(s)
    statements = fence.reservation.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict):
        observe(s, service)
    assert calls == 2
    assert len(fence.reservation.dml(statements)) == 1  # fixture injection only
    assert events == ["begin", "rollback", "close"] and not sessions[0].in_transaction()
    with s.session.begin():
        assert fence.reservation.snapshot(s) == before


def test_connection_enlisted_by_begin_hook_refuses_before_shared_scope_read(armed, monkeypatch):
    original = TrackedSession.begin

    def enlisted(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        session.connection()
        return result

    def forbidden(*_args):
        pytest.fail("shared repository read must not occur")

    monkeypatch.setattr(TrackedSession, "begin", enlisted)
    monkeypatch.setattr(CasdoorRoleAttemptRepository, "_read_attempt_scope", forbidden)
    with pytest.raises(CasdoorRoleIntentConflict):
        observe(armed)


def test_source_has_only_reads_and_service_owns_cleanup():
    import repositories.casdoor_role_attempt_fence_reader_repository_extend as repository
    import services.casdoor_role_attempt_fence_reader_service_extend as service

    repo_calls = {
        node.attr
        for node in ast.walk(ast.parse(Path(repository.__file__).read_text()))
        if isinstance(node, ast.Attribute)
    }
    assert not repo_calls & {
        "reserve",
        "mark_dispatch_unconfirmed",
        "add",
        "flush",
        "commit",
        "rollback",
        "begin_nested",
        "update",
        "insert",
        "delete",
        "replace_rbac",
        "acquire",
        "delay",
        "send",
        "login",
    }
    service_calls = {
        node.attr for node in ast.walk(ast.parse(Path(service.__file__).read_text())) if isinstance(node, ast.Attribute)
    }
    assert {"begin", "rollback", "close"} <= service_calls
    assert not service_calls & {"commit", "flush", "begin_nested", "reserve", "mark_dispatch_unconfirmed"}
