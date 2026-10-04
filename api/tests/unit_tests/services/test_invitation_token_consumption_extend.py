"""Offline P1 contract tests using deterministic Redis outcomes, never a Redis service."""

import json
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pytest
from redis.crc import key_slot

from extensions.redis_names import serialize_redis_name
from services import account_adapters as adapters
from services.account_activation_service import AccountActivationService, InvitationTokenStore
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import InvitationLookup

TOKEN = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"
WORKSPACE = "33333333-3333-4333-8333-333333333333"
ISSUANCE = "44444444-4444-4444-8444-444444444444"
LIFECYCLE = "55555555-5555-4555-8555-555555555555"
OPERATION = "66666666-6666-4666-8666-666666666666"
OTHER_OPERATION = "77777777-7777-4777-8777-777777777777"


def payload():
    return {
        "account_id": ACCOUNT,
        "email": "invitee@example.com",
        "workspace_id": WORKSPACE,
        "role": "editor",
        "requires_setup": False,
        "invitation_authority": {
            "schema_version": 1,
            "issuance_id": ISSUANCE,
            "lifecycle_id": LIFECYCLE,
            "lifecycle_epoch": 1,
            "token_digest": sha256(TOKEN.encode()).hexdigest(),
            "join_id_at_issue": None,
        },
    }


def raw_payload():
    return json.dumps(payload()).encode()


class FakeRedis:
    """Model explicit script outcomes; this is not a Lua interpreter or concurrency proof."""

    def __init__(self, prefix=""):
        self.prefix = prefix
        self.entries = {}
        self.calls = []
        self.fail_after = False
        self.prefix_reads = 0
        self.token_key = serialize_redis_name(f"member_invite:token:{TOKEN}", prefix).encode()
        self.entries[self.token_key] = ("string", raw_payload(), 60000)

    def _get_prefix(self):
        self.prefix_reads += 1
        return self.prefix

    def get(self, key):
        return self.entries[serialize_redis_name(key, self.prefix).encode()][1]

    def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        assert all(type(key) is bytes for key in args[:numkeys])
        token = self.entries.get(args[0])
        if script == adapters._INVITATION_OBSERVE:
            if token is None:
                return [b"absent_or_expired"]
            kind, raw, ttl = token
            if kind != "string" or ttl == -1 or len(raw) > 8192:
                return [b"invalid"]
            if ttl <= 0:
                return [b"absent_or_expired"]
            return [b"observed", raw, ttl]
        receipt_key, expected_raw, expected_receipt = args[1:4]
        assert key_slot(args[0]) == key_slot(receipt_key)
        receipt = self.entries.get(receipt_key)
        exact_receipt = (
            receipt is not None and receipt[0] == "string" and receipt[1] == expected_receipt and receipt[2] > 0
        )
        if script == adapters._INVITATION_READBACK:
            if receipt is not None:
                return b"confirmed" if exact_receipt and token is None else b"unknown"
            return (
                b"not_confirmed"
                if token and token[0] == "string" and token[1] == expected_raw and token[2] > 0
                else b"unknown"
            )
        assert script == adapters._INVITATION_CONSUME
        if receipt is not None:
            return b"replayed" if exact_receipt and token is None else b"receipt_conflict"
        if token is None or token[0] != "string" or token[2] <= 0:
            return b"unknown"
        if token[1] != expected_raw:
            return b"mismatch"
        self.entries[receipt_key] = ("string", expected_receipt, args[4] * 1000)
        del self.entries[args[0]]
        if self.fail_after:
            raise RuntimeError("SECRET raw payload and token")
        return b"consumed"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket

    denied = Mock(side_effect=AssertionError("network forbidden"))
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield
    denied.assert_not_called()


def observed(prefix=""):
    redis = FakeRedis(prefix)
    store = RedisInvitationTokenStore(redis=redis)
    result = store.observe_versioned_invitation(TOKEN)
    assert result.status == "observed"
    assert result.observation is not None
    return redis, store, result.observation


def consume(store, observation, **kwargs):
    return store.consume_versioned_invitation(
        observation, operation_id=kwargs.get("operation_id", OPERATION), receipt_ttl_seconds=kwargs.get("ttl", 60)
    )


def read(store, observation, operation_id=OPERATION):
    return store.read_invitation_consumption(observation, operation_id=operation_id)


def test_versioned_payload_legacy_find_and_both_adapters_ignore_extra():
    from services.account_service import _invitation_adapter

    redis, store, observation = observed()
    old = store.find(InvitationLookup(None, None, TOKEN))
    assert old.account_id == ACCOUNT and old.requires_setup is False
    data = payload()
    data["future_unknown"] = {"anything": True}
    raw = json.dumps(data).encode()
    assert adapters._invitation_token_adapter.validate_json(raw) == old
    assert _invitation_adapter.validate_json(raw) == {
        k: v for k, v in data.items() if k not in {"invitation_authority", "future_unknown"}
    }
    assert observation.payload.invitation_authority.join_id_at_issue is None
    assert observation._raw == raw_payload() and observation.ttl_ms == 60000
    assert TOKEN not in repr(observation) and "invitee" not in repr(observation)
    assert repr(store.observe_versioned_invitation(TOKEN)) == "InvitationObservationResult(status='observed')"
    assert len(redis.calls) == 2


@pytest.mark.parametrize(
    "token",
    [
        None,
        3,
        True,
        TOKEN.upper().replace("1", "A", 1),
        " " + TOKEN,
        TOKEN + " ",
        "{" + TOKEN + "}",
        TOKEN.replace("-", ""),
        TOKEN.replace("4111", "1111"),
        "x" * 513,
        InvitationLookup(None, "x@y.com", TOKEN),
    ],
)
def test_token_only_canonical_uuid4_before_io(token):
    redis = FakeRedis()
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(token).status == "invalid"
    assert redis.calls == [] and redis.prefix_reads == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "alias"),
        ("workspace_id", WORKSPACE.upper().replace("3", "A", 1)),
        ("role", ""),
        ("role", "汉" * 86),
        ("role", 1),
        ("email", "wrong"),
        ("email", "a" * 250 + "@a.com"),
        ("requires_setup", 1),
        ("requires_setup", "false"),
        ("email", None),
        ("role", "\ud800"),
    ],
)
def test_strict_legacy_field_types(field, value):
    data = payload()
    data[field] = value
    assert adapters._parse_versioned_invitation(json.dumps(data).encode(), TOKEN) == ("invalid", None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("issuance_id", "bad"),
        ("lifecycle_id", None),
        ("lifecycle_epoch", True),
        ("lifecycle_epoch", 0),
        ("lifecycle_epoch", 2**53),
        ("lifecycle_epoch", 1.0),
        ("join_id_at_issue", ""),
        ("join_id_at_issue", 1),
        ("token_digest", "0" * 64),
        ("token_digest", sha256(TOKEN.encode()).hexdigest().upper()),
        ("token_digest", None),
    ],
)
def test_strict_authority_types_and_digest(field, value):
    data = payload()
    data["invitation_authority"][field] = value
    assert adapters._parse_versioned_invitation(json.dumps(data).encode(), TOKEN) == ("invalid", None)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"{}",
        b"[]",
        b"null",
        b" " * 8193,
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1,"x":2}',
        b'{"x":{"y":{"z":{"a":1}}}}',
        b"[" * 2000 + b"]" * 2000,
        "not bytes",
    ],
)
def test_strict_json_shape_encoding_size_depth_duplicate(raw):
    assert adapters._parse_versioned_invitation(raw, TOKEN) == ("invalid", None)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("change", ["unknown", "missing", "duplicate"])
def test_exact_keys(nested, change):
    data = payload()
    target = data["invitation_authority"] if nested else data
    field = "lifecycle_id" if nested else "account_id"
    if change == "unknown":
        target["extra"] = 1
    if change == "missing":
        del target[field]
    raw = json.dumps(data).encode()
    if change == "duplicate":
        raw = raw.replace(f'"{field}":'.encode(), f'"{field}": "duplicate", "{field}":'.encode())
    assert adapters._parse_versioned_invitation(raw, TOKEN) == ("invalid", None)


@pytest.mark.parametrize("version,status", [(None, "capability_stale"), (2, "unsupported_version"), (1, "observed")])
def test_versions_preserve_original(version, status):
    redis = FakeRedis()
    data = payload()
    if version is None:
        del data["invitation_authority"]
    else:
        data["invitation_authority"]["schema_version"] = version
    raw = json.dumps(data).encode()
    redis.entries[redis.token_key] = ("string", raw, 60000)
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN).status == status
    assert redis.entries[redis.token_key][1] == raw
    assert len(redis.calls) == 1


def test_unknown_version_with_future_authority_field_is_unsupported():
    redis = FakeRedis()
    data = payload()
    data["invitation_authority"]["schema_version"] = 2
    data["invitation_authority"]["future_field"] = "future_value"
    raw = json.dumps(data).encode()
    redis.entries[redis.token_key] = ("string", raw, 60000)
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN).status == "unsupported_version"
    assert redis.entries[redis.token_key][1] == raw
    assert len(redis.calls) == 1


@pytest.mark.parametrize("prefix", ["", "ordinary", "a{tag}", "a{}", "unmatched{", "unmatched}", "中文", "a{未闭合"])
def test_physical_key_once_binary_slot_all_prefixes(prefix, monkeypatch):
    serializer = Mock(wraps=serialize_redis_name)
    monkeypatch.setattr(adapters, "serialize_redis_name", serializer)
    redis, store, observation = observed(prefix)
    assert redis.prefix_reads == 1
    serializer.assert_called_once_with(f"member_invite:token:{TOKEN}", prefix)
    assert redis.calls[0][2] == (redis.token_key,)
    assert consume(store, observation).status == "consumed"
    assert serializer.call_count == 2
    key, receipt_key = redis.calls[-1][2][:2]
    assert type(receipt_key) is bytes and key != receipt_key and key_slot(key) == key_slot(receipt_key)
    base = serialize_redis_name(f"member_invite:receipt:{observation.key_digest}:{OPERATION}:", prefix).encode()
    assert receipt_key.startswith(base) and len(receipt_key) == len(base) + 2
    suffix = int.from_bytes(receipt_key[-2:])
    assert all(key_slot(base + i.to_bytes(2, "big")) != key_slot(key) for i in range(suffix))
    receipt = json.loads(redis.entries[receipt_key][1])
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
    assert TOKEN not in str(receipt) and "email" not in receipt
    assert redis.entries[receipt_key][1] == json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    assert len(redis.calls) == 2


@pytest.mark.parametrize("failure", ["prefix", "slot", "other_store", "same_fields", "tamper"])
def test_binding_and_slot_fail_before_io(failure, monkeypatch):
    redis, store, observation = observed()
    if failure == "prefix":
        redis.prefix = "drift"
    elif failure == "slot":
        monkeypatch.setattr(adapters, "key_slot", lambda key: 1 if key == redis.token_key else 2)
    elif failure == "other_store":
        store = RedisInvitationTokenStore(redis=redis)
    elif failure == "same_fields":
        observation = replace(observation)
    else:
        object.__setattr__(observation, "_raw", b"tampered")
    assert consume(store, observation).status == "unavailable"
    assert read(store, observation).status == "unavailable"
    assert len(redis.calls) == 1


@pytest.mark.parametrize("ttl", [True, 0, -1, 604801, 1.5, "60"])
def test_receipt_ttl_invalid_before_io(ttl):
    redis, store, observation = observed()
    assert consume(store, observation, ttl=ttl).status == "unavailable"
    assert len(redis.calls) == 1


@pytest.mark.parametrize("operation", ["bad", None, "{" + OPERATION + "}", OPERATION.replace("-", "")])
def test_operation_canonical_before_io(operation):
    redis, store, observation = observed()
    assert consume(store, observation, operation_id=operation).status == "unavailable"
    assert read(store, observation, operation).status == "unavailable"
    assert len(redis.calls) == 1


@pytest.mark.parametrize(
    "entry,status",
    [
        (None, "absent_or_expired"),
        (("list", b"x", 500), "invalid"),
        (("string", b"x", -1), "invalid"),
        (("string", b"x", 0), "absent_or_expired"),
        (("string", b"x", -2), "absent_or_expired"),
        (("string", b"x" * 8193, 50), "invalid"),
    ],
)
def test_observe_wrong_type_persistent_expired(entry, status):
    redis = FakeRedis()
    if entry is None:
        redis.entries.clear()
    else:
        redis.entries[redis.token_key] = entry
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN).status == status
    assert len(redis.calls) == 1


@pytest.mark.parametrize(
    "response,status",
    [
        (None, "unknown"),
        ([], "unknown"),
        ([b"observed", raw_payload(), True], "invalid"),
        ([b"observed", raw_payload(), 0], "invalid"),
        ([b"observed", "decoded", 1], "invalid"),
        ([b"observed"], "unknown"),
        ([b"invalid", b"extra"], "unknown"),
    ],
)
def test_malformed_wire_responses(response, status):
    redis = FakeRedis()
    redis.eval = Mock(return_value=response)
    assert RedisInvitationTokenStore(redis=redis).observe_versioned_invitation(TOKEN).status == status
    redis.eval.assert_called_once()


def test_consume_replay_retention_and_different_operation_unknown():
    redis, store, observation = observed()
    assert read(store, observation).status == "not_confirmed"
    assert consume(store, observation, ttl=1).status == "consumed"
    receipt_key = redis.calls[-1][2][1]
    receipt = redis.entries[receipt_key]
    assert consume(store, observation, ttl=604800).status == "replayed"
    assert redis.entries[receipt_key] == receipt and receipt[2] == 1000
    assert read(store, observation).status == "confirmed"
    assert consume(store, observation, operation_id=OTHER_OPERATION).status == "unknown"
    assert read(store, observation, OTHER_OPERATION).status == "unknown"
    assert len(redis.calls) == 7


@pytest.mark.parametrize(
    "change,status",
    [
        ("whitespace", "mismatch"),
        ("role", "mismatch"),
        ("missing", "unknown"),
        ("persistent", "unknown"),
        ("expired", "unknown"),
        ("type", "unknown"),
    ],
)
def test_only_exact_live_token_consumed(change, status):
    redis, store, observation = observed()
    original = redis.entries[redis.token_key]
    if change == "missing":
        del redis.entries[redis.token_key]
    elif change == "whitespace":
        redis.entries[redis.token_key] = ("string", original[1] + b" ", 60000)
    elif change == "role":
        redis.entries[redis.token_key] = ("string", original[1].replace(b"editor", b"admin"), 60000)
    elif change == "type":
        redis.entries[redis.token_key] = ("hash", original[1], 60000)
    else:
        redis.entries[redis.token_key] = ("string", original[1], -1 if change == "persistent" else 0)
    before = redis.entries.copy()
    assert consume(store, observation).status == status
    assert read(store, observation).status == "unknown"
    assert redis.entries == before and len(redis.calls) == 3


@pytest.mark.parametrize(
    "corrupt", ["missing", "broken", "expired", "persistent", "type", "other_operation", "token_present"]
)
def test_receipt_readback_conflicts(corrupt):
    redis, store, observation = observed()
    assert consume(store, observation).status == "consumed"
    key = redis.calls[-1][2][1]
    original = redis.entries[key]
    if corrupt == "missing":
        del redis.entries[key]
    elif corrupt == "token_present":
        redis.entries[redis.token_key] = ("string", observation._raw, 60000)
    elif corrupt == "other_operation":
        redis.entries[key] = ("string", original[1].replace(OPERATION.encode(), OTHER_OPERATION.encode()), 60000)
    elif corrupt == "broken":
        redis.entries[key] = ("string", b"{}", 60000)
    elif corrupt == "type":
        redis.entries[key] = ("hash", original[1], 60000)
    else:
        redis.entries[key] = ("string", original[1], 0 if corrupt == "expired" else -1)
    before = redis.entries.copy()
    assert read(store, observation).status == "unknown"
    assert consume(store, observation).status == ("unknown" if corrupt == "missing" else "receipt_conflict")
    assert redis.entries == before and len(redis.calls) == 4


def test_ack_lost_once_then_same_store_readback():
    redis, store, observation = observed()
    redis.fail_after = True
    assert consume(store, observation).status == "unknown"
    assert len(redis.calls) == 2
    assert read(store, observation).status == "confirmed"
    assert consume(store, observation).status == "replayed"
    assert len(redis.calls) == 4


@pytest.mark.parametrize("phase", ["setup", "body", "teardown"])
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_sensitive_failure_sanitized_outside_context(phase, error, monkeypatch):
    import libs.sensitive_redis as privacy

    redis, store, observation = observed()
    left = []

    @contextmanager
    def failed_context():
        try:
            if phase == "setup":
                raise error("SECRET")
            yield
            if phase == "teardown":
                raise error("SECRET")
        finally:
            left.append(True)

    monkeypatch.setattr(privacy, "sensitive_redis_call", failed_context)
    redis.eval = Mock(return_value=b"unknown")
    if phase == "body":
        redis.eval = Mock(side_effect=error("SECRET"))
    if error is RuntimeError:
        with pytest.raises(adapters._InvitationStorageUncertain) as caught:
            store._strong_eval("PRIVATE SCRIPT", (b"SECRET KEY",), b"SECRET RAW")
        assert str(caught.value) == "invitation_storage_uncertain"
    else:
        with pytest.raises(error) as caught:
            store._strong_eval("PRIVATE SCRIPT", (b"SECRET KEY",), b"SECRET RAW")
        assert "SECRET" not in str(caught.value)
    assert left == [True]
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    if error is RuntimeError:
        assert consume(store, observation).status == "unknown"


def test_privacy_setup_failure_zero_eval(monkeypatch):
    import libs.sensitive_redis as privacy

    @contextmanager
    def failed():
        raise RuntimeError("SECRET")
        yield

    redis, store, observation = observed()
    monkeypatch.setattr(privacy, "sensitive_redis_call", failed)
    assert store.observe_versioned_invitation(TOKEN).status == "unavailable"
    assert consume(store, observation).status == "unknown"
    assert read(store, observation).status == "unknown"
    assert len(redis.calls) == 1


def test_legacy_store_service_zero_business_calls():
    legacy = Mock(spec=InvitationTokenStore)
    owners = {
        name: Mock()
        for name in ("accounts", "workspace_policy", "eligibility", "membership_cache", "member_access_sync")
    }
    service = AccountActivationService(tokens=legacy, **owners)
    assert service.observe_versioned_invitation(TOKEN).status == "unavailable"
    assert (
        service.consume_versioned_invitation(None, operation_id=OPERATION, receipt_ttl_seconds=60).status
        == "unavailable"
    )
    assert service.read_invitation_consumption(None, operation_id=OPERATION).status == "unavailable"
    assert legacy.mock_calls == []
    assert all(owner.mock_calls == [] for owner in owners.values())


def test_service_strong_delegation_only():
    redis, store, observation = observed()
    owners = {
        name: Mock()
        for name in ("accounts", "workspace_policy", "eligibility", "membership_cache", "member_access_sync")
    }
    service = AccountActivationService(tokens=store, **owners)
    assert service.observe_versioned_invitation(TOKEN).status == "observed"
    assert (
        service.consume_versioned_invitation(observation, operation_id=OPERATION, receipt_ttl_seconds=60).status
        == "consumed"
    )
    assert service.read_invitation_consumption(observation, operation_id=OPERATION).status == "confirmed"
    assert all(owner.mock_calls == [] for owner in owners.values())
    assert len(redis.calls) == 4


def test_lua_order_and_no_replay_ttl_refresh():
    consume_script = adapters._INVITATION_CONSUME
    assert consume_script.index("return 'replayed'") < consume_script.index("redis.call('SET'")
    assert consume_script.index("redis.call('SET'") < consume_script.index("redis.call('DEL'")
    assert consume_script.count("redis.call('SET'") == 1
    assert "'EX', ARGV[3], 'NX'" in consume_script
    assert "redis.call('PTTL', KEYS[1]) <= 0" in consume_script
    assert "redis.call('GET', KEYS[1]) ~= ARGV[1]" in consume_script
    assert "redis.call('STRLEN', KEYS[1]) > 8192" in adapters._INVITATION_OBSERVE


@pytest.mark.parametrize("value", [2, True, 1.0])
def test_nested_payload_tampering_rejected(value):
    redis, store, observation = observed()
    object.__setattr__(observation.payload.invitation_authority, "lifecycle_epoch", value)
    assert consume(store, observation).status == "unavailable"
    assert read(store, observation).status == "unavailable"
    assert len(redis.calls) == 1


def test_parser_exact_size_limit_and_valid_join_epoch():
    data = payload()
    data["invitation_authority"]["join_id_at_issue"] = OPERATION
    data["invitation_authority"]["lifecycle_epoch"] = 2**53 - 1
    data["role"] = "r" * 255
    raw = json.dumps(data).encode()
    assert adapters._parse_versioned_invitation(raw.ljust(8192), TOKEN)[0] == "observed"
    assert adapters._parse_versioned_invitation(raw.ljust(8193), TOKEN)[0] == "invalid"


@pytest.mark.parametrize("ttl", [1, 604800])
def test_explicit_retention_bounds(ttl):
    redis, store, observation = observed()
    assert consume(store, observation, ttl=ttl).status == "consumed"
    assert redis.entries[redis.calls[-1][2][1]][2] == ttl * 1000


@pytest.mark.parametrize("response", [None, b"unexpected", "consumed", [b"consumed"]])
def test_uncertain_consume_readback_responses_no_retry(response):
    redis, store, observation = observed()
    redis.eval = Mock(return_value=response)
    assert consume(store, observation).status == "unknown"
    assert read(store, observation).status == "unknown"
    assert redis.eval.call_count == 2
