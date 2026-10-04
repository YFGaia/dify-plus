"""Independent P3J checks: fresh readback is not consumption or authorization."""

import inspect
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from redis import Redis
from redis._parsers import _RESP2Parser
from redis.backoff import NoBackoff
from redis.connection import Connection, ConnectionPool
from redis.retry import Retry

from extensions.ext_redis import RedisClientWrapper
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIntegrationExtend,
    CasdoorIntentKind,
    CasdoorNamespaceExtend,
    CasdoorOperationState,
    CasdoorSyncIntentExtend,
)
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository
from services import account_adapters
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import (
    InvitationConsumptionReadback,
    InvitationConsumptionRecovery,
    InvitationRecoveryBinding,
)
from tests.unit_tests.services import test_invitation_token_consumption_extend as p1

PREFIX = "independent-p3j"
NAMESPACE = "88888888-8888-4888-8888-888888888888"
IDENTITY = "99999999-9999-4999-8999-999999999999"


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _bulk(value):
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


def _parse_command(buffer):
    end = buffer.find(b"\r\n")
    if end < 0 or buffer[:1] != b"*":
        return None
    count, offset, parts = int(buffer[1:end]), end + 2, []
    for _ in range(count):
        length_end = buffer.find(b"\r\n", offset)
        if length_end < 0 or buffer[offset : offset + 1] != b"$":
            return None
        length = int(buffer[offset + 1 : length_end])
        start = length_end + 2
        finish = start + length
        if len(buffer) < finish + 2:
            return None
        parts.append(bytes(buffer[start:finish]))
        offset = finish + 2
    del buffer[:offset]
    return parts


class _ReadbackPeer:
    """Minimal RESP peer; readback inspects an immutable image and never mutates it."""

    def __init__(self, entries, token_key, receipt_key, payload, receipt):
        self.entries = dict(entries)
        self.token_key, self.receipt_key = token_key, receipt_key
        self.payload, self.receipt = payload, receipt
        self.frames = []
        self.sockets = []
        self.in_business = False

    def socket(self):
        result = _OfflineSocket(self)
        self.sockets.append(result)
        return result

    def respond(self, socket, frame):
        self.frames.append(frame)
        command = frame[0].upper()
        if command == b"CLIENT":
            return b"+OK\r\n"
        if command != b"EVAL":
            raise AssertionError("readback path sent a non-EVAL business command")
        self.in_business = True
        if (
            frame[1] != account_adapters._INVITATION_READBACK.encode()
            or frame[2] != b"2"
            or tuple(frame[3:5]) != (self.token_key, self.receipt_key)
            or frame[5] != self.payload
            or frame[6] != self.receipt
        ):
            return _bulk(b"unknown")
        token, receipt = self.entries.get(self.token_key), self.entries.get(self.receipt_key)
        confirmed = (
            token is None
            and receipt is not None
            and receipt[0] == "string"
            and receipt[1] == self.receipt
            and type(receipt[2]) is int
            and receipt[2] > 0
        )
        return _bulk(b"confirmed" if confirmed else b"unknown")


class _OfflineSocket:
    def __init__(self, peer):
        self.peer = peer
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.timeout = 1.0
        self.closed = False

    def sendall(self, value):
        self.incoming.extend(value)
        while frame := _parse_command(self.incoming):
            self.outgoing.extend(self.peer.respond(self, frame))

    def recv(self, count):
        if not self.outgoing:
            raise TimeoutError("offline peer has no queued response")
        response = bytes(self.outgoing[:count])
        del self.outgoing[:count]
        return response

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def shutdown(self, _how):
        return None

    def close(self):
        self.closed = True
        self.peer.in_business = False


@pytest.fixture(autouse=True)
def local_settings(config_overrides, monkeypatch):
    config_overrides(REDIS_KEY_PREFIX=PREFIX)
    monkeypatch.setattr(account_adapters, "monotonic", lambda: 100.0)


@pytest.fixture
def recovered_facts(sqlite_session_factory):
    """Produce and consume via the unchanged P1 contract, then rebuild SQL facts."""
    repository = InvitationAuthorityRepository()
    producer_redis = p1.FakeRedis(PREFIX)
    producer = RedisInvitationTokenStore(redis=producer_redis)
    with sqlite_session_factory() as session:
        lifecycle = repository.set_lifecycle_state(
            session, account_id=p1.ACCOUNT, workspace_id=p1.WORKSPACE, state="active"
        )
        payload = p1.payload()
        payload["invitation_authority"]["lifecycle_id"] = lifecycle.lifecycle_id
        payload["invitation_authority"]["lifecycle_epoch"] = lifecycle.epoch
        issuance = repository.record_issuance(session, payload_json=compact(payload))
        producer_redis.entries[producer_redis.token_key] = (
            "string",
            issuance.payload_json.encode(),
            60000,
        )
        observation = producer.observe_versioned_invitation(p1.TOKEN).observation
        assert observation is not None

        integration = CasdoorIntegrationExtend()
        session.add(integration)
        session.flush()
        namespace = CasdoorNamespaceExtend(
            id=NAMESPACE,
            integration_id=integration.id,
            expected_issuer="https://issuer.example.invalid",
            organization="local-test",
            application="local-test",
            client_id="local-test",
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
            organization="local-test",
            application="local-test",
            client_id="local-test",
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
        keys, expected_receipt = producer._consumption_arguments(observation, operation.id)
        operation.desired_json = compact({"issuance_id": issuance.issuance_id, "receipt": json.loads(expected_receipt)})
        session.commit()

    # Read the immutable P2/P3J facts from another SQL session before the P1 consume.
    with sqlite_session_factory() as session:
        stored_operation = session.get(CasdoorSyncIntentExtend, operation.id)
        stored_issuance = repository.get_issuance(session, issuance_id=issuance.issuance_id)
        assert stored_operation is not None and stored_issuance is not None
        operation_id = stored_operation.id
        payload_json, payload_digest = stored_issuance.payload_json, stored_issuance.payload_digest
        expected_receipt = compact(json.loads(stored_operation.desired_json)["receipt"]).encode()
        assert p1.TOKEN not in stored_issuance.payload_json
        assert p1.TOKEN not in stored_operation.desired_json
        before = (
            stored_issuance.state,
            stored_issuance.payload_digest,
            stored_operation.id,
            stored_operation.desired_json,
            stored_operation.operation_state,
        )
    assert p1.TOKEN not in payload_json and p1.TOKEN.encode() not in expected_receipt
    assert (
        producer.consume_versioned_invitation(observation, operation_id=operation_id, receipt_ttl_seconds=60).status
        == "consumed"
    )
    assert producer_redis.token_key not in producer_redis.entries

    # The new store has no process-local observation. The test bearer remains only in this request.
    binding = InvitationRecoveryBinding(NAMESPACE, IDENTITY, p1.ACCOUNT, p1.WORKSPACE)
    recovery = InvitationConsumptionRecovery(
        namespace_id=NAMESPACE,
        identity_id=IDENTITY,
        account_id=p1.ACCOUNT,
        workspace_id=p1.WORKSPACE,
        payload_json=payload_json,
        payload_digest=payload_digest,
        operation_id=operation_id,
        expected_receipt=expected_receipt,
        deadline_monotonic=105.0,
    )
    return SimpleNamespace(
        binding=binding,
        recovery=recovery,
        before=before,
        entries=dict(producer_redis.entries),
        token_key=keys[0],
        receipt_key=keys[1],
        issuance_id=issuance.issuance_id,
        operation_id=operation_id,
    )


def _new_store(monkeypatch, facts):
    peer = _ReadbackPeer(
        facts.entries,
        facts.token_key,
        facts.receipt_key,
        facts.recovery.payload_json.encode(),
        facts.recovery.expected_receipt,
    )
    monkeypatch.setattr(Connection, "_connect", lambda _connection: peer.socket())
    pool = ConnectionPool(
        connection_class=Connection,
        parser_class=_RESP2Parser,
        retry=Retry(NoBackoff(), 1),
        socket_timeout=1,
    )
    wrapper = RedisClientWrapper()
    wrapper.initialize(Redis(connection_pool=pool))
    original_retry = Retry.call_with_retry

    def trapped_retry(self, *args, **kwargs):
        if peer.in_business:
            raise AssertionError("client retry callback entered during readback exchange")
        return original_retry(self, *args, **kwargs)

    monkeypatch.setattr(Retry, "call_with_retry", trapped_retry)
    store = RedisInvitationTokenStore(redis=wrapper)
    return store, peer, pool


def _activation(store):
    dependencies = [Mock() for _ in range(5)]
    service = AccountActivationService(
        tokens=store,
        accounts=dependencies[0],
        workspace_policy=dependencies[1],
        eligibility=dependencies[2],
        membership_cache=dependencies[3],
        member_access_sync=dependencies[4],
    )
    return service, dependencies


def test_fresh_store_confirms_only_exact_p1_receipt_and_changes_no_authority(
    sqlite_session_factory, monkeypatch, recovered_facts, caplog, capsys
):
    facts = recovered_facts
    store, peer, pool = _new_store(monkeypatch, facts)
    service, dependencies = _activation(store)
    before_entries = dict(peer.entries)

    result = service.recover_invitation_consumption(facts.recovery, token=p1.TOKEN, trusted_attempt=facts.binding)

    assert result == InvitationConsumptionReadback("confirmed")
    assert len([frame for frame in peer.frames if frame[0].upper() == b"EVAL"]) == 1
    frame = next(frame for frame in peer.frames if frame[0].upper() == b"EVAL")
    assert frame[1] == account_adapters._INVITATION_READBACK.encode()
    assert frame[2] == b"2" and tuple(frame[3:5]) == (facts.token_key, facts.receipt_key)
    assert frame[5] == facts.recovery.payload_json.encode()
    assert frame[6] == facts.recovery.expected_receipt
    assert not store._observations and peer.entries == before_entries
    assert all(socket.closed for socket in peer.sockets) and not pool._in_use_connections
    assert all(not dependency.mock_calls for dependency in dependencies)
    assert p1.TOKEN not in repr(facts.recovery) and p1.TOKEN not in repr(result)
    assert all(
        secret not in repr(result) + repr(facts.recovery) + caplog.text + str(capsys.readouterr())
        for secret in (p1.TOKEN, "invitee@example.com", PREFIX, "member_invite:")
    )
    assert all(command not in account_adapters._INVITATION_READBACK.upper() for command in ("SET", "DEL", "EXPIRE"))
    with sqlite_session_factory() as session:
        issuance = InvitationAuthorityRepository().get_issuance(session, issuance_id=facts.issuance_id)
        operation = session.get(CasdoorSyncIntentExtend, facts.operation_id)
        assert (
            issuance.state,
            issuance.payload_digest,
            operation.id,
            operation.desired_json,
            operation.operation_state,
        ) == facts.before
        assert operation.operation_state == CasdoorOperationState.UNKNOWN


@pytest.mark.parametrize("field", ["namespace_id", "identity_id", "account_id", "workspace_id"])
def test_wrong_trusted_attempt_binding_is_rejected_before_redis(monkeypatch, recovered_facts, field):
    facts = recovered_facts
    store, peer, _ = _new_store(monkeypatch, facts)
    service, _ = _activation(store)
    wrong = replace(facts.binding, **{field: str(uuid4())})

    result = service.recover_invitation_consumption(facts.recovery, token=p1.TOKEN, trusted_attempt=wrong)

    assert result.status == "unavailable" and peer.frames == [] and not store._observations


def test_wrong_bearer_or_wrong_prefix_never_confirms(monkeypatch, config_overrides, recovered_facts):
    facts = recovered_facts
    store, peer, _ = _new_store(monkeypatch, facts)
    service, _ = _activation(store)
    assert (
        service.recover_invitation_consumption(
            facts.recovery, token=p1.OTHER_OPERATION, trusted_attempt=facts.binding
        ).status
        == "unavailable"
    )
    assert peer.frames == []

    config_overrides(REDIS_KEY_PREFIX="changed-prefix")
    assert (
        service.recover_invitation_consumption(facts.recovery, token=p1.TOKEN, trusted_attempt=facts.binding).status
        == "unavailable"
    )
    assert peer.frames == []


@pytest.mark.parametrize("field", ["payload_digest", "payload_json", "expected_receipt", "operation_id"])
def test_descriptor_integrity_failure_stays_unavailable_before_io(monkeypatch, recovered_facts, field):
    facts = recovered_facts
    store, peer, _ = _new_store(monkeypatch, facts)
    service, _ = _activation(store)
    changes = {
        "payload_digest": "0" * 64,
        "payload_json": facts.recovery.payload_json + " ",
        "expected_receipt": facts.recovery.expected_receipt + b" ",
        "operation_id": str(uuid4()),
    }

    result = service.recover_invitation_consumption(
        replace(facts.recovery, **{field: changes[field]}),
        token=p1.TOKEN,
        trusted_attempt=facts.binding,
    )

    assert result.status == "unavailable" and peer.frames == []


@pytest.mark.parametrize("state", ["wrong_receipt", "token_present", "absent_receipt"])
def test_nonmatching_receipt_or_present_token_stays_unknown_without_write(monkeypatch, recovered_facts, state):
    facts = recovered_facts
    store, peer, _ = _new_store(monkeypatch, facts)
    if state == "wrong_receipt":
        peer.entries[facts.receipt_key] = ("string", b'{"status":"consumed"}', 30000)
    elif state == "token_present":
        peer.entries[facts.token_key] = ("string", facts.recovery.payload_json.encode(), 30000)
    else:
        peer.entries.pop(facts.receipt_key)
    before = dict(peer.entries)
    result = store.recover_invitation_consumption(facts.recovery, token=p1.TOKEN, trusted_attempt=facts.binding)
    assert result.status == "unknown"
    assert peer.entries == before
    business = [frame for frame in peer.frames if frame[0].upper() == b"EVAL"]
    assert len(business) == 1 and business[0][1] == account_adapters._INVITATION_READBACK.encode()


def test_recovery_cannot_enter_consume_path_or_choose_operation_id(monkeypatch, recovered_facts):
    facts = recovered_facts
    store, peer, _ = _new_store(monkeypatch, facts)
    service, dependencies = _activation(store)
    assert UUID(facts.operation_id).version == 4
    assert "operation_id" not in inspect.signature(service.recover_invitation_consumption).parameters
    assert not store._observations

    result = service.recover_invitation_consumption(facts.recovery, token=p1.TOKEN, trusted_attempt=facts.binding)
    consume = store.consume_versioned_invitation(
        facts.recovery, operation_id=facts.operation_id, receipt_ttl_seconds=60
    )
    assert result.status == "confirmed" and consume.status == "unavailable"
    assert not store._observations and len([frame for frame in peer.frames if frame[0].upper() == b"EVAL"]) == 1
    assert all(not dependency.mock_calls for dependency in dependencies)
