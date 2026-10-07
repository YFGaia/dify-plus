"""Recovery from committed SQLite facts with real redis-py and offline RESP sockets."""

import inspect
import json
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from redis.connection import Connection, ConnectionPool
from redis.crc import key_slot

from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorNamespaceExtend,
    CasdoorOperationState,
    CasdoorSyncIntentExtend,
)
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository
from services import account_adapters as adapters
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import (
    InvitationConsumptionRecovery,
    InvitationConsumptionRecoveryStore,
    InvitationRecoveryBinding,
    VersionedInvitationObservation,
    VersionedInvitationTokenStore,
)
from tests.unit_tests.services import test_invitation_publication_transport_extend as transport
from tests.unit_tests.services import test_invitation_token_consumption_extend as p1

NAMESPACE = "88888888-8888-4888-8888-888888888888"
IDENTITY = "99999999-9999-4999-8999-999999999999"
PREFIX = "one"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def snapshot(session):
    models = (Issuance, Lifecycle, CasdoorSyncIntentExtend)
    return [list(session.execute(sa.select(*model.__table__.columns)).all()) for model in models]


@pytest.fixture(autouse=True)
def local_settings(config_overrides, monkeypatch):
    config_overrides(REDIS_KEY_PREFIX=PREFIX)
    monkeypatch.setattr(adapters, "monotonic", lambda: 100.0)


@pytest.fixture
def durable(sqlite_session_factory):
    """Test-only durable producer. No production producer/finalizer is introduced."""
    repo = InvitationAuthorityRepository()
    redis = p1.FakeRedis(PREFIX)
    original = RedisInvitationTokenStore(redis=redis)
    with sqlite_session_factory() as session:
        lifecycle = repo.set_lifecycle_state(session, account_id=p1.ACCOUNT, workspace_id=p1.WORKSPACE, state="active")
        data = p1.payload()
        data["invitation_authority"]["lifecycle_id"] = lifecycle.lifecycle_id
        data["invitation_authority"]["lifecycle_epoch"] = lifecycle.epoch
        stored = repo.record_issuance(session, payload_json=canonical(data))
        redis.entries[redis.token_key] = ("string", stored.payload_json.encode(), 60000)
        observed = original.observe_versioned_invitation(p1.TOKEN).observation
        assert observed is not None
        integration = CasdoorIntegrationExtend()
        session.add(integration)
        session.flush()
        namespace = CasdoorNamespaceExtend(
            id=NAMESPACE,
            integration_id=integration.id,
            expected_issuer="https://issuer.example.invalid",
            organization="offline",
            application="offline",
            client_id="offline",
            core_fingerprint="a" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = CasdoorConfigRevisionExtend(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="b" * 64,
            browser_frontend_url="https://issuer.example.invalid",
            backend_api_url="https://issuer.example.invalid",
            expected_issuer="https://issuer.example.invalid",
            organization="offline",
            application="offline",
            client_id="offline",
            button_text="Sign in",
            default_workspace_id=p1.WORKSPACE,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        session.add(revision)
        session.flush()
        operation = CasdoorSyncIntentExtend(
            namespace_id=NAMESPACE,
            identity_id=IDENTITY,
            account_id=p1.ACCOUNT,
            workspace_id=p1.WORKSPACE,
            revision_id=revision.id,
            generation=1,
            ownership_epoch=1,
            fence_epoch=0,
            kind=CasdoorIntentKind.INVITATION_FINALIZE,
            scope_digest="c" * 64,
            idempotency_key="d" * 64,
            desired_json="{}",
            operation_state=CasdoorOperationState.UNKNOWN,
        )
        session.add(operation)
        session.flush()
        keys, receipt = original._consumption_arguments(observed, operation.id)
        operation.desired_json = canonical({"issuance_id": stored.issuance_id, "receipt": json.loads(receipt)})
        operation_id = operation.id
        session.commit()
    # Independent SQL session proves immutable issuance + server operation preceded consume.
    with sqlite_session_factory() as session:
        assert session.get(CasdoorSyncIntentExtend, operation_id).desired_json
        assert repo.get_issuance(session, issuance_id=p1.ISSUANCE).payload_json == stored.payload_json
    assert (
        original.consume_versioned_invitation(observed, operation_id=operation_id, receipt_ttl_seconds=60).status
        == "consumed"
    )
    assert redis.token_key not in redis.entries
    # Drop the issuing store/observation; reconstruct solely from committed SQL.
    del original, observed
    with sqlite_session_factory() as session:
        row = session.get(CasdoorSyncIntentExtend, operation_id)
        record = repo.get_issuance(session, issuance_id=p1.ISSUANCE)
        recovery = InvitationConsumptionRecovery(
            namespace_id=row.namespace_id,
            identity_id=row.identity_id,
            account_id=row.account_id,
            workspace_id=row.workspace_id,
            payload_json=record.payload_json,
            payload_digest=record.payload_digest,
            operation_id=row.id,
            expected_receipt=canonical(json.loads(row.desired_json)["receipt"]).encode(),
            deadline_monotonic=110.0,
        )
        before = snapshot(session)
        assert p1.TOKEN not in record.payload_json and p1.TOKEN not in row.desired_json
    binding = InvitationRecoveryBinding(NAMESPACE, IDENTITY, p1.ACCOUNT, p1.WORKSPACE)
    return SimpleNamespace(recovery=recovery, binding=binding, keys=keys, redis=redis, before=before)


class RecoveryWire(transport.Wire):
    def __init__(self, durable, *, mode="exact"):
        super().__init__(eval_mode=mode)
        self.entries = dict(durable.redis.entries)
        self.mode = mode
        self.expected_keys = durable.keys

    def answer(self, sock, frame):
        if frame[0].upper() != b"EVAL":
            return super().answer(sock, frame)
        self.frames.append(frame)
        self.business = True
        self.eval_responses += 1
        assert frame[1] == adapters._INVITATION_READBACK.encode()
        assert frame[2] == b"2" and len(frame) == 7
        assert tuple(frame[3:5]) == self.expected_keys
        assert key_slot(frame[3]) == key_slot(frame[4])
        if self.mode in ("MOVED", "ASK", "TRYAGAIN", "READONLY"):
            return b"-" + self.mode.encode() + b" offline\r\n"
        if self.mode in ("lost_ack", "interrupt", "exit"):
            sock.read_failure = {
                "lost_ack": TimeoutError("private raw socket data"),
                "interrupt": KeyboardInterrupt("private raw socket data"),
                "exit": SystemExit(73),
            }[self.mode]
            return b""
        if self.mode == "malformed":
            return transport.resp([b"confirmed"])
        if self.mode == "integer":
            return transport.resp(1)
        token, receipt = self.entries.get(frame[3]), self.entries.get(frame[4])
        if receipt is not None:
            result = (
                b"confirmed"
                if receipt[0] == "string" and receipt[1] == frame[6] and receipt[2] > 0 and token is None
                else b"unknown"
            )
        else:
            result = (
                b"not_confirmed"
                if token and token[0] == "string" and token[1] == frame[5] and token[2] > 0
                else b"unknown"
            )
        return transport.resp(result)


def harness(durable, monkeypatch, *, mode="exact", topology="standalone"):
    wire = RecoveryWire(durable, mode=mode)
    wrapper, raw, pool = transport.client_for(wire, monkeypatch, topology=topology)
    if topology == "cluster":
        cluster = wrapper._client
        node = next(iter(cluster.nodes_manager.slots_cache.values()))
        cluster.nodes_manager.slots_cache = {key_slot(durable.keys[0]): node}
    raw.execute_command = Mock(side_effect=AssertionError("ordinary command forbidden"))
    wrapper.eval = Mock(side_effect=AssertionError("ordinary wrapper EVAL forbidden"))
    wrapper.pipeline = Mock(side_effect=AssertionError("pipeline forbidden"))
    return RedisInvitationTokenStore(redis=wrapper), wire, pool


def recover(store, durable, **kwargs):
    return store.recover_invitation_consumption(
        kwargs.get("recovery", durable.recovery),
        token=kwargs.get("token", p1.TOKEN),
        trusted_attempt=kwargs.get("binding", durable.binding),
    )


def service(store):
    deps = [Mock() for _ in range(5)]
    result = AccountActivationService(
        tokens=store,
        accounts=deps[0],
        workspace_policy=deps[1],
        eligibility=deps[2],
        membership_cache=deps[3],
        member_access_sync=deps[4],
    )
    return result, deps


@pytest.mark.parametrize("topology", ["standalone", "ssl", "sentinel", "sentinel_ssl", "cluster"])
def test_fresh_store_exact_durable_facts_confirm_read_only(durable, sqlite_session_factory, monkeypatch, topology):
    store, wire, pool = harness(durable, monkeypatch, topology=topology)
    before = dict(wire.entries)
    activation, deps = service(store)
    assert (
        activation.recover_invitation_consumption(
            durable.recovery, token=p1.TOKEN, trusted_attempt=durable.binding
        ).status
        == "confirmed"
    )
    assert not store._observations and wire.entries == before
    assert len(wire.business_frames()) == 1 and wire.releases == [True]
    assert all(sock.closed and 0 < sock.timeout <= 1 for sock in wire.sockets)
    assert not pool._in_use_connections
    assert all(not dependency.mock_calls for dependency in deps)
    with sqlite_session_factory() as session:
        assert snapshot(session) == durable.before
        assert session.get(Issuance, p1.ISSUANCE).state == "issued"
        assert (
            session.get(CasdoorSyncIntentExtend, durable.recovery.operation_id).operation_state
            == CasdoorOperationState.UNKNOWN
        )
    assert recover(store, durable).status == "confirmed"
    assert len(wire.business_frames()) == 2 and wire.entries == before  # distinct explicit read requests


@pytest.mark.parametrize(
    "token",
    [
        None,
        3,
        True,
        "",
        p1.OTHER_OPERATION,
        " " + p1.TOKEN,
        p1.TOKEN + " ",
        p1.TOKEN.replace("-", ""),
        "{" + p1.TOKEN + "}",
        p1.TOKEN.replace("4111", "1111"),
        b"token",
    ],
)
def test_wrong_or_noncanonical_bearer_never_opens_connection(durable, monkeypatch, token):
    store, wire, _ = harness(durable, monkeypatch)
    assert recover(store, durable, token=token).status == "unavailable"
    assert not wire.frames and not store._observations


@pytest.mark.parametrize("field", ["namespace_id", "identity_id", "account_id", "workspace_id"])
@pytest.mark.parametrize("boundary", ["service", "adapter"])
def test_trusted_attempt_must_match_every_binding(durable, monkeypatch, field, boundary):
    store, wire, _ = harness(durable, monkeypatch)
    target = service(store)[0] if boundary == "service" else store
    assert recover(target, durable, binding=replace(durable.binding, **{field: str(uuid4())})).status == "unavailable"
    assert not wire.frames


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace_id", "invalid"),
        ("identity_id", None),
        ("account_id", p1.OTHER_OPERATION),
        ("workspace_id", p1.OTHER_OPERATION),
        ("operation_id", p1.OTHER_OPERATION),
        ("operation_id", "invalid"),
        ("operation_id", p1.OPERATION.replace("4666", "1666")),
        ("payload_json", b"{}"),
        ("payload_json", "{}"),
        ("payload_digest", "0" * 64),
        ("payload_digest", None),
        ("expected_receipt", "{}"),
        ("expected_receipt", b"{}"),
    ],
)
def test_bad_descriptor_never_reads(durable, monkeypatch, field, value):
    store, wire, _ = harness(durable, monkeypatch)
    assert recover(store, durable, recovery=replace(durable.recovery, **{field: value})).status == "unavailable"
    assert not wire.frames


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "status",
        "operation_id",
        "issuance_id",
        "lifecycle_id",
        "lifecycle_epoch",
        "account_id",
        "workspace_id",
        "join_id_at_issue",
        "token_digest",
        "payload_digest",
        "key_digest",
    ],
)
def test_every_receipt_fact_exactly_bound(durable, monkeypatch, field):
    store, wire, _ = harness(durable, monkeypatch)
    receipt = json.loads(durable.recovery.expected_receipt)
    receipt[field] = "wrong"
    assert (
        recover(store, durable, recovery=replace(durable.recovery, expected_receipt=canonical(receipt).encode())).status
        == "unavailable"
    )
    assert not wire.frames


@pytest.mark.parametrize("mode", ["duplicate", "extra", "spacing", "missing", "invalid"])
def test_receipt_shape_and_canonical_bytes_are_required(durable, monkeypatch, mode):
    store, wire, _ = harness(durable, monkeypatch)
    raw = durable.recovery.expected_receipt
    data = json.loads(raw)
    if mode == "duplicate":
        raw = b'{"status":"consumed",' + raw[1:]
    elif mode == "extra":
        raw = canonical({**data, "extra": 1}).encode()
    elif mode == "spacing":
        raw = json.dumps(data).encode()
    elif mode == "missing":
        del data["key_digest"]
        raw = canonical(data).encode()
    else:
        raw = b"\xff"
    assert recover(store, durable, recovery=replace(durable.recovery, expected_receipt=raw)).status == "unavailable"
    assert not wire.frames


@pytest.mark.parametrize(
    "field", ["token_digest", "issuance_id", "lifecycle_id", "lifecycle_epoch", "join_id_at_issue"]
)
def test_payload_changes_with_recomputed_digest_still_fail_receipt_binding(durable, monkeypatch, field):
    store, wire, _ = harness(durable, monkeypatch)
    data = json.loads(durable.recovery.payload_json)
    data["invitation_authority"][field] = (
        2 if field == "lifecycle_epoch" else "0" * 64 if field == "token_digest" else str(uuid4())
    )
    raw = canonical(data)
    assert (
        recover(
            store,
            durable,
            recovery=replace(durable.recovery, payload_json=raw, payload_digest=sha256(raw.encode()).hexdigest()),
        ).status
        == "unavailable"
    )
    assert not wire.frames


def test_payload_bytes_and_prefix_drift_never_confirm(durable, monkeypatch, config_overrides):
    store, wire, _ = harness(durable, monkeypatch)
    raw = durable.recovery.payload_json + " "
    assert recover(store, durable, recovery=replace(durable.recovery, payload_json=raw)).status == "unavailable"
    config_overrides(REDIS_KEY_PREFIX="different")
    assert recover(store, durable).status == "unavailable"
    assert not wire.frames


@pytest.mark.parametrize(
    "state",
    ["absent", "expired", "persistent", "wrong_bytes", "wrong_type", "token_present", "token_only", "both_absent"],
)
def test_unknown_redis_states_never_write_or_reconsume(durable, monkeypatch, state):
    store, wire, _ = harness(durable, monkeypatch)
    token, key = durable.keys
    if state in ("absent", "both_absent", "token_only"):
        wire.entries.pop(key)
    elif state in ("expired", "persistent"):
        wire.entries[key] = ("string", durable.recovery.expected_receipt, 0 if state == "expired" else -1)
    elif state == "wrong_bytes":
        wire.entries[key] = ("string", b"wrong", 6000)
    elif state == "wrong_type":
        wire.entries[key] = ("hash", durable.recovery.expected_receipt, 6000)
    if state in ("token_present", "token_only"):
        wire.entries[token] = ("string", durable.recovery.payload_json.encode(), 1000)
    before = dict(wire.entries)
    assert recover(store, durable).status == "unknown"
    assert wire.entries == before and len(wire.business_frames()) == 1 and wire.releases == [True]


@pytest.mark.parametrize(
    "mode", ["lost_ack", "partial", "MOVED", "ASK", "TRYAGAIN", "READONLY", "malformed", "integer"]
)
def test_uncertain_transport_single_attempt_sanitized(durable, monkeypatch, mode, caplog, capsys):
    store, wire, pool = harness(durable, monkeypatch, mode=mode)
    result = recover(store, durable)
    assert result.status == "unknown"
    assert len(wire.business_frames()) == (0 if mode == "partial" else 1)
    assert wire.releases == [True] and not pool._in_use_connections
    output = repr(result) + caplog.text + str(capsys.readouterr()) + repr(durable.recovery)
    assert all(
        secret not in output
        for secret in (p1.TOKEN, "invitee@example.com", "member_invite:", "private raw socket data")
    )


@pytest.mark.parametrize("mode,kind", [("interrupt", KeyboardInterrupt), ("exit", SystemExit)])
def test_control_signals_preserved_after_clean_release(durable, monkeypatch, mode, kind):
    store, wire, pool = harness(durable, monkeypatch, mode=mode)
    with pytest.raises(kind) as caught:
        recover(store, durable)
    assert "private" not in str(caught.value) and caught.value.__context__ is None
    assert wire.releases == [True] and not pool._in_use_connections
    assert len(wire.business_frames()) == 1


@pytest.mark.parametrize("deadline", [None, True, float("nan"), float("inf"), 99.0, 100.0, 131.0])
def test_invalid_or_unbounded_deadline_rejected_before_io(durable, monkeypatch, deadline):
    store, wire, _ = harness(durable, monkeypatch)
    assert (
        recover(store, durable, recovery=replace(durable.recovery, deadline_monotonic=deadline)).status == "unavailable"
    )
    assert not wire.frames


@pytest.mark.parametrize("phase", ["lease", "reply", "after_read"])
def test_elapsed_deadline_prevents_late_confirmation(durable, monkeypatch, phase):
    store, wire, _ = harness(durable, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(adapters, "monotonic", lambda: clock[0])
    if phase == "lease":
        original = ConnectionPool.get_connection

        def late_lease(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            clock[0] = 111.0
            return result

        monkeypatch.setattr(ConnectionPool, "get_connection", late_lease)
    elif phase == "after_read":
        original_read = Connection.read_response

        def late_read(self, *args, **kwargs):
            result = original_read(self, *args, **kwargs)
            if wire.business:
                clock[0] = 111.0
            return result

        monkeypatch.setattr(Connection, "read_response", late_read)
    else:
        original = wire.answer

        def late_reply(sock, frame):
            result = original(sock, frame)
            if frame[0] == b"EVAL":
                clock[0] = 111.0
            return result

        monkeypatch.setattr(wire, "answer", late_reply)
    assert recover(store, durable).status == "unknown"
    assert len(wire.business_frames()) == (0 if phase == "lease" else 1)
    assert wire.releases == [True]


def test_recovery_never_grants_observation_or_consume_capability(durable, monkeypatch):
    store, wire, _ = harness(durable, monkeypatch)
    assert isinstance(store, InvitationConsumptionRecoveryStore)
    assert isinstance(store, VersionedInvitationTokenStore)
    assert recover(store, durable).status == "confirmed"
    assert not store._observations
    assert (
        store.consume_versioned_invitation(
            durable.recovery, operation_id=durable.recovery.operation_id, receipt_ttl_seconds=60
        ).status
        == "unavailable"
    )
    status, payload = adapters._parse_versioned_invitation(durable.recovery.payload_json.encode(), p1.TOKEN)
    assert status == "observed"
    forged = VersionedInvitationObservation(
        payload,
        60000,
        durable.recovery.payload_digest,
        sha256(durable.keys[0]).hexdigest(),
        durable.recovery.payload_json.encode(),
        durable.keys[0],
        PREFIX,
    )
    assert (
        store.consume_versioned_invitation(
            forged, operation_id=durable.recovery.operation_id, receipt_ttl_seconds=60
        ).status
        == "unavailable"
    )
    assert store.read_invitation_consumption(forged, operation_id=durable.recovery.operation_id).status == "unavailable"
    assert len(wire.business_frames()) == 1 and not store._observations


def test_optional_capability_and_server_owned_operation_boundary(durable):
    legacy = SimpleNamespace(find=Mock(), revoke=Mock())
    activation, deps = service(legacy)
    assert recover(activation, durable).status == "unavailable"
    assert not any(dependency.mock_calls for dependency in deps)
    assert "operation_id" not in inspect.signature(activation.recover_invitation_consumption).parameters
    with pytest.raises(TypeError):
        activation.recover_invitation_consumption(
            durable.recovery, token=p1.TOKEN, trusted_attempt=durable.binding, operation_id=p1.OTHER_OPERATION
        )
    assert not isinstance(legacy, InvitationConsumptionRecoveryStore)


@pytest.mark.parametrize("phase", ["enter", "exit", "release", "disconnect"])
def test_telemetry_and_cleanup_failure_cannot_confirm(durable, monkeypatch, phase):
    from libs import sensitive_redis

    store, wire, pool = harness(durable, monkeypatch)
    if phase in ("enter", "exit"):
        original = sensitive_redis.sensitive_redis_call

        @contextmanager
        def failing_context():
            with original():
                if phase == "enter":
                    raise RuntimeError("private raw telemetry")
                yield
                raise RuntimeError("private raw telemetry")

        monkeypatch.setattr(sensitive_redis, "sensitive_redis_call", failing_context)
    elif phase == "release":
        original = ConnectionPool.release

        def failing_release(self, connection):
            original(self, connection)
            raise RuntimeError("private raw release")

        monkeypatch.setattr(ConnectionPool, "release", failing_release)
    else:
        original = Connection.disconnect

        def failing_disconnect(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("private raw disconnect")

        monkeypatch.setattr(Connection, "disconnect", failing_disconnect)
    assert recover(store, durable).status == "unknown"
    assert len(wire.business_frames()) == (0 if phase == "enter" else 1)
    assert not pool._in_use_connections
    assert all(connection._sock is None for connection in pool._available_connections)


def test_request_remaining_time_caps_socket_io(durable, monkeypatch):
    store, wire, _ = harness(durable, monkeypatch)
    assert recover(store, durable, recovery=replace(durable.recovery, deadline_monotonic=100.25)).status == "confirmed"
    assert all(0 < sock.timeout <= 0.25 for sock in wire.sockets)


@pytest.mark.parametrize("phase", ["deadline", "disconnect", "interrupt"])
def test_suppressing_telemetry_cannot_hide_incomplete_read_or_cleanup(durable, monkeypatch, phase):
    from libs import sensitive_redis

    store, wire, _ = harness(durable, monkeypatch, mode="interrupt" if phase == "interrupt" else "exact")
    original = sensitive_redis.sensitive_redis_call

    @contextmanager
    def suppressing_context():
        with original():
            try:
                yield
            except BaseException:
                pass

    monkeypatch.setattr(sensitive_redis, "sensitive_redis_call", suppressing_context)
    if phase == "deadline":
        original_answer = wire.answer

        def late_response(sock, frame):
            result = original_answer(sock, frame)
            if frame[0] == b"EVAL":
                monkeypatch.setattr(adapters, "monotonic", lambda: 111.0)
            return result

        monkeypatch.setattr(wire, "answer", late_response)
    elif phase == "disconnect":
        original_disconnect = Connection.disconnect

        def fail_disconnect(self, *args, **kwargs):
            original_disconnect(self, *args, **kwargs)
            raise RuntimeError("private disconnect details")

        monkeypatch.setattr(Connection, "disconnect", fail_disconnect)
    if phase == "interrupt":
        with pytest.raises(KeyboardInterrupt, match="^invitation recovery interrupted$"):
            recover(store, durable)
    else:
        assert recover(store, durable).status == "unknown"
    assert len(wire.business_frames()) == 1 and wire.releases == [True]


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_preflight_control_signal_is_sanitized(durable, monkeypatch, kind):
    store, wire, _ = harness(durable, monkeypatch)
    monkeypatch.setattr(store._redis, "_get_prefix", Mock(side_effect=kind("private prefix details")))
    with pytest.raises(kind) as caught:
        recover(store, durable)
    assert "private" not in str(caught.value) and caught.value.__context__ is None
    assert not wire.frames


def test_disconnect_failure_before_socket_clear_never_pools_dirty_connection(durable, monkeypatch):
    store, wire, pool = harness(durable, monkeypatch)
    original = Connection.disconnect
    dirty = []

    def fail_before_clear(self, *args, **kwargs):
        if self._sock is not None and wire.business:
            dirty.append(self)
            raise RuntimeError("private dirty socket")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Connection, "disconnect", fail_before_clear)
    assert recover(store, durable).status == "unknown"
    assert len(dirty) == 2 and wire.releases == [False]
    assert not pool._in_use_connections and not pool._available_connections
    assert len(wire.business_frames()) == 1
    monkeypatch.setattr(Connection, "disconnect", original)
    for connection in dirty:
        connection.disconnect()


@pytest.mark.parametrize("which", ["recovery", "binding"])
def test_structural_duck_types_never_become_trusted_descriptors(durable, monkeypatch, which):
    store, wire, _ = harness(durable, monkeypatch)
    kwargs = {
        which: SimpleNamespace(
            **{
                name: getattr(durable.recovery if which == "recovery" else durable.binding, name)
                for name in (durable.recovery if which == "recovery" else durable.binding).__dataclass_fields__
            }
        )
    }
    assert recover(store, durable, **kwargs).status == "unavailable"
    assert not wire.frames
