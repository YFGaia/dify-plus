"""Finite encrypted RP workflows; exact-value atomic transitions prevent replay.

Keys share one Redis hash slot. The private wrapper prefixes normal commands;
Lua keys must use that same serializer explicitly. No public status, account
latest index, provider request or durable login authority belongs here.
"""

import base64
import re

_MAX_BYTES = 64 * 1024
_KINDS = frozenset({"handoff", "state", "retry", "observation"})
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_CREATE_PAIR = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""
_TRANSITION = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return 0 end
if ARGV[4] == 'nx' and redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'PX', math.min(ttl, tonumber(ARGV[3]) * 1000))
redis.call('DEL', KEYS[1])
return 1
"""
_CONSUME = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('PTTL', KEYS[1]) <= 0 then return 0 end
return redis.call('DEL', KEYS[1])
"""
_RETRY = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 or redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
ttl = math.min(ttl, tonumber(ARGV[4]) * 1000)
redis.call('SET', KEYS[1], ARGV[2], 'PX', ttl)
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[3], ARGV[3], 'PX', ttl)
return 1
"""


def canonical_opaque(value):
    if type(value) is not str or not _OPAQUE.fullmatch(value):
        raise ValueError("casdoor_rp_logout_invalid")
    decoded = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError("casdoor_rp_logout_invalid")
    return value


class CasdoorRPLogoutRepository:
    def __init__(self, client, *, key_serializer=None):
        self._client = client
        if key_serializer is None:
            from extensions.redis_names import serialize_redis_name

            key_serializer = serialize_redis_name
        self._serialize = key_serializer

    @staticmethod
    def _key(kind, opaque):
        if kind not in _KINDS:
            raise ValueError("casdoor_rp_logout_invalid")
        return "casdoor:rp:{casdoor-rp}:" + kind + ":" + canonical_opaque(opaque)

    def _lua_key(self, kind, opaque):
        return self._serialize(self._key(kind, opaque), self._client._get_prefix())

    @staticmethod
    def _value(raw):
        if type(raw) is not str or not raw or len(raw.encode()) > _MAX_BYTES:
            raise ValueError("casdoor_rp_logout_invalid")
        return raw

    @staticmethod
    def _ttl(ttl):
        # Same bounded browser transaction lifetime as the existing auth owner.
        if type(ttl) is not int or not 0 < ttl <= 300:
            raise ValueError("casdoor_rp_logout_invalid")
        return ttl

    def read(self, kind, opaque):
        raw = self._client.get(self._key(kind, opaque))
        if raw is None:
            return None
        if type(raw) is bytes:
            raw = raw.decode("utf-8")
        return self._value(raw)

    def create_pair(
        self,
        *,
        first_kind,
        first_id,
        first_raw,
        second_kind,
        second_id,
        second_raw,
        ttl,
    ):
        return (
            self._client.eval(
                _CREATE_PAIR,
                2,
                self._lua_key(first_kind, first_id),
                self._lua_key(second_kind, second_id),
                self._value(first_raw),
                self._value(second_raw),
                self._ttl(ttl),
            )
            == 1
        )

    def create(self, kind, opaque, raw, ttl):
        return self._client.set(self._key(kind, opaque), self._value(raw), ex=self._ttl(ttl), nx=True) is True

    def publish_observation(self, opaque, raw, ttl):
        return self._client.set(self._key("observation", opaque), self._value(raw), ex=self._ttl(ttl), nx=False) is True

    def transition(
        self,
        *,
        source_kind,
        source_id,
        expected,
        target_kind,
        target_id,
        target_raw,
        ttl,
        replace=False,
    ):
        return (
            self._client.eval(
                _TRANSITION,
                2,
                self._lua_key(source_kind, source_id),
                self._lua_key(target_kind, target_id),
                self._value(expected),
                self._value(target_raw),
                self._ttl(ttl),
                "replace" if replace else "nx",
            )
            == 1
        )

    def consume(self, kind, opaque, expected):
        return self._client.eval(_CONSUME, 1, self._lua_key(kind, opaque), self._value(expected)) == 1

    def retry(self, *, retry_id, expected, replacement, old_handoff, new_handoff, handoff_raw, ttl):
        return (
            self._client.eval(
                _RETRY,
                3,
                self._lua_key("retry", retry_id),
                self._lua_key("handoff", old_handoff),
                self._lua_key("handoff", new_handoff),
                self._value(expected),
                self._value(replacement),
                self._value(handoff_raw),
                self._ttl(ttl),
            )
            == 1
        )
