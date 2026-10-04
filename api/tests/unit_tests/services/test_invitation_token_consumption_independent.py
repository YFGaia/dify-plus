"""Fresh offline checks for the invitation token-store boundary."""

import json
from hashlib import sha256
from unittest.mock import Mock

import pytest
from redis.crc import key_slot

from extensions.redis_names import serialize_redis_name
from services import account_adapters
from services.account_adapters import RedisInvitationTokenStore

TOKEN = "11111111-1111-4111-8111-111111111111"
OPERATION = "66666666-6666-4666-8666-666666666666"


def invitation_record(version=1):
    return {
        "account_id": "22222222-2222-4222-8222-222222222222",
        "email": "member@example.test",
        "workspace_id": "33333333-3333-4333-8333-333333333333",
        "role": "editor",
        "requires_setup": False,
        "invitation_authority": {
            "schema_version": version,
            "issuance_id": "44444444-4444-4444-8444-444444444444",
            "lifecycle_id": "55555555-5555-4555-8555-555555555555",
            "lifecycle_epoch": 4,
            "token_digest": sha256(TOKEN.encode("utf-8")).hexdigest(),
            "join_id_at_issue": None,
        },
    }


class ReplyRedis:
    """Return controlled EVAL replies while retaining the exact wire arguments."""

    def __init__(self, prefix, raw):
        self.prefix = prefix
        self.raw = raw
        self.prefix_reads = 0
        self.calls = []
        self.eval_error = None

    def _get_prefix(self):
        self.prefix_reads += 1
        return self.prefix

    def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        if self.eval_error:
            raise self.eval_error
        if script == account_adapters._INVITATION_OBSERVE:
            return [b"observed", self.raw, 32000]
        if script == account_adapters._INVITATION_CONSUME:
            return b"consumed"
        if script == account_adapters._INVITATION_READBACK:
            return b"confirmed"
        raise AssertionError("unexpected script")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    import socket

    denied = Mock(side_effect=AssertionError("network is disabled for this verification"))
    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield
    denied.assert_not_called()


def test_future_integer_schema_is_classified_before_v1_authority_shape():
    record = invitation_record(version=9)
    record["invitation_authority"]["issuer_key_id"] = "planned-field"
    raw = json.dumps(record, separators=(",", ":")).encode("utf-8")
    redis = ReplyRedis("", raw)

    result = RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN)

    assert result.status == "unsupported_version"
    assert result.observation is None
    assert len(redis.calls) == 1
    assert redis.calls[0][2] == (f"member_invite:token:{TOKEN}".encode(),)
    assert redis.prefix_reads == 1
    assert redis.raw == raw


def test_v1_still_rejects_future_authority_fields_and_legacy_envelope_is_stale():
    extra = invitation_record()
    extra["invitation_authority"]["issuer_key_id"] = "unexpected-in-v1"
    redis = ReplyRedis("", json.dumps(extra).encode())
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN).status == "invalid"

    legacy = {key: value for key, value in invitation_record().items() if key != "invitation_authority"}
    stale_redis = ReplyRedis("", json.dumps(legacy).encode())
    stale = RedisInvitationTokenStore(redis=stale_redis).observe_versioned_invitation(TOKEN)
    assert stale.status == "capability_stale" and stale.observation is None


@pytest.mark.parametrize("prefix", ["", "tenant:", "tenant:{open", "租户/区域"])
def test_consume_uses_physical_binary_keys_in_the_installed_cluster_slot(prefix):
    raw = json.dumps(invitation_record(), separators=(",", ":")).encode("utf-8")
    redis = ReplyRedis(prefix, raw)
    store = RedisInvitationTokenStore(redis=redis)
    observed = store.observe_versioned_invitation(TOKEN)
    assert observed.status == "observed" and observed.observation is not None
    observation = observed.observation

    consumed = store.consume_versioned_invitation(observation, operation_id=OPERATION, receipt_ttl_seconds=73)
    readback = store.read_invitation_consumption(observation, operation_id=OPERATION)

    assert consumed.status == "consumed" and readback.status == "confirmed"
    assert redis.prefix_reads == 3
    assert len(redis.calls) == 3
    observe_args = redis.calls[0][2]
    consume_script, key_count, wire = redis.calls[1]
    assert key_count == 2 and consume_script == account_adapters._INVITATION_CONSUME
    token_key, receipt_key, sent_raw, receipt_bytes, ttl_seconds = wire
    assert observe_args == (token_key,)
    assert type(token_key) is bytes and type(receipt_key) is bytes
    assert token_key == serialize_redis_name(f"member_invite:token:{TOKEN}", prefix).encode()
    assert (
        receipt_key != token_key
        and len(receipt_key)
        == len(serialize_redis_name(f"member_invite:receipt:{observation.key_digest}:{OPERATION}:", prefix).encode())
        + 2
    )
    assert key_slot(token_key) == key_slot(receipt_key)
    assert sent_raw == raw and ttl_seconds == 73
    receipt = json.loads(receipt_bytes)
    assert set(receipt) == {
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
    }
    assert receipt["status"] == "consumed" and receipt["operation_id"] == OPERATION
    assert "member@example.test" not in receipt_bytes.decode() and TOKEN not in receipt_bytes.decode()
    assert redis.calls[2][2][:3] == (token_key, receipt_key, raw)


def test_observation_is_private_and_bound_to_its_issuing_store():
    redis = ReplyRedis("prefixed:", json.dumps(invitation_record()).encode())
    store = RedisInvitationTokenStore(redis=redis)
    result = store.observe_versioned_invitation(TOKEN)
    observation = result.observation
    assert result.status == "observed" and observation is not None
    assert observation.ttl_ms == 32000
    assert observation.payload.invitation_authority.join_id_at_issue is None
    assert TOKEN not in repr(observation) and "member@example.test" not in repr(observation)

    other_redis = ReplyRedis("prefixed:", redis.raw)
    other_store = RedisInvitationTokenStore(redis=other_redis)
    assert (
        other_store.consume_versioned_invitation(observation, operation_id=OPERATION, receipt_ttl_seconds=10).status
        == "unavailable"
    )
    assert other_redis.calls == []


@pytest.mark.parametrize("ttl", [0, -1, 604801, True, 1.0])
def test_receipt_retention_must_be_an_explicit_bounded_integer_before_io(ttl):
    redis = ReplyRedis("", json.dumps(invitation_record()).encode())
    store = RedisInvitationTokenStore(redis=redis)
    observation = store.observe_versioned_invitation(TOKEN).observation
    assert observation is not None
    call_count = len(redis.calls)

    result = store.consume_versioned_invitation(observation, operation_id=OPERATION, receipt_ttl_seconds=ttl)

    assert result.status == "unavailable"
    assert len(redis.calls) == call_count


def test_storage_exception_is_collapsed_to_safe_status():
    redis = ReplyRedis("", json.dumps(invitation_record()).encode())
    redis.eval_error = RuntimeError(f"private token={TOKEN} raw={redis.raw!r}")
    result = RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN)
    assert result.status == "unavailable" and TOKEN not in repr(result)
    assert len(redis.calls) == 1
