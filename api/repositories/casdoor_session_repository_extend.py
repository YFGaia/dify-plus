"""Short-lived provenance records; never a login or provider-logout authority.

Encrypted values are swapped/consumed by exact-value CAS. No account latest index,
provider request, SQL transaction or latest-index authority belongs here. The
optional encrypted RP hint is accepted only by the session/RP owners.
"""

import re

_MAX_RECORD_BYTES = 64 * 1024
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SWAP = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ttl)
return 1
"""
_CONSUME = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class CasdoorSessionRepository:
    def __init__(self, client):
        self._client = client

    def _lua_key(self, opaque):
        from extensions.redis_names import serialize_redis_name

        # The actual private wrapper prefixes normal GET/SET, but Lua keys need
        # the same serializer explicitly. A raw offline wire has an empty prefix.
        prefix = self._client._get_prefix() if hasattr(self._client, "_get_prefix") else ""
        return serialize_redis_name(self._key(opaque), prefix)

    @staticmethod
    def _key(opaque):
        if type(opaque) is not str or not _OPAQUE.fullmatch(opaque):
            raise ValueError("casdoor_source_invalid")
        return "casdoor:session:" + opaque

    def create(self, opaque, record, ttl):
        if type(record) is not str or len(record.encode()) > _MAX_RECORD_BYTES or type(ttl) is not int or ttl <= 0:
            raise ValueError("casdoor_source_invalid")
        return self._client.set(self._key(opaque), record, ex=ttl, nx=True) is True

    def read(self, opaque):
        value = self._client.get(self._key(opaque))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if type(value) is not str or len(value.encode()) > _MAX_RECORD_BYTES:
            raise ValueError("casdoor_source_invalid")
        return value

    def replace(self, opaque, expected, replacement):
        return self._client.eval(_SWAP, 1, self._lua_key(opaque), expected, replacement) == 1

    def consume(self, opaque, expected):
        return self._client.eval(_CONSUME, 1, self._lua_key(opaque), expected) == 1
