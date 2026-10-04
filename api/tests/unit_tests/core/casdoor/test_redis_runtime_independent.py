"""Fresh independent fake-wire checks for the private Redis request runtime."""

from types import SimpleNamespace

import pytest
from redis.connection import Connection

from core.casdoor import redis_runtime


class Tick:
    value = 100.0

    def __call__(self):
        return self.value


class RespSocket:
    """Small RESP2 socket double that records real redis-py wire commands."""

    def __init__(self, clock):
        self.clock = clock
        self.outgoing = bytearray()
        self.frames = []
        self.deadlines = []
        self.is_closed = False
        self.leave_open_on_close = False
        self.force_timeout_on = None
        self.close_calls = 0

    def settimeout(self, seconds):
        self.timeout = seconds
        self.deadlines.append(seconds)

    def gettimeout(self):
        return self.timeout

    def sendall(self, packet):
        size_end = packet.index(b"\r\n")
        count = int(packet[1:size_end])
        pos = size_end + 2
        parts = []
        for _ in range(count):
            assert packet[pos : pos + 1] == b"$"
            end = packet.index(b"\r\n", pos)
            nbytes = int(packet[pos + 1 : end])
            pos = end + 2
            parts.append(packet[pos : pos + nbytes])
            pos += nbytes + 2
        command = tuple(parts)
        self.frames.append(command)
        self.clock.value += 0.0
        if command and command[0] == self.force_timeout_on:
            raise TimeoutError("simulated private command timeout")
        if command[0] in (b"AUTH", b"SELECT", b"CLIENT", b"SETNAME", b"SETINFO"):
            reply = b"+OK\r\n"
        elif command[0] == b"PING":
            reply = b"+PONG\r\n"
        else:
            reply = b":1\r\n"
        self.outgoing.extend(reply)

    def recv(self, length):
        if not self.outgoing:
            raise TimeoutError("empty fake receive")
        chunk = bytes(self.outgoing[:length])
        del self.outgoing[:length]
        return chunk

    def recv_into(self, target):
        data = self.recv(len(target))
        target[: len(data)] = data
        return len(data)

    def shutdown(self, how):
        pass

    def close(self):
        self.close_calls += 1
        if not self.leave_open_on_close:
            self.is_closed = True

    def fileno(self):
        return -1 if self.is_closed else 42


def redis_settings(**changes):
    base = {
        "REDIS_USE_SENTINEL": False,
        "REDIS_USE_CLUSTERS": False,
        "REDIS_ENABLE_CLIENT_SIDE_CACHE": False,
        "REDIS_HOST": "offline.invalid",
        "REDIS_PORT": 6379,
        "REDIS_USERNAME": None,
        "REDIS_PASSWORD": None,
        "REDIS_DB": 0,
        "REDIS_KEY_PREFIX": "case:independent:",
        "REDIS_SERIALIZATION_PROTOCOL": 2,
        "REDIS_USE_SSL": False,
        "REDIS_SSL_CERT_REQS": "CERT_REQUIRED",
        "REDIS_SSL_CA_CERTS": None,
        "REDIS_SSL_CERTFILE": None,
        "REDIS_SSL_KEYFILE": None,
    }
    base.update(changes)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_transport(monkeypatch):
    tick = Tick()
    opened = []

    def supply_socket(self):
        wire = RespSocket(tick)
        opened.append(wire)
        return wire

    monkeypatch.setattr(Connection, "_connect", supply_socket)
    return SimpleNamespace(clock=tick, sockets=opened)


def factory(fake_transport, **settings):
    return redis_runtime.CasdoorRedisRuntimeFactory(
        redis_settings(**settings), clock=fake_transport.clock, command_cap=5.0, cleanup_reserve=1.0
    )


def test_open_is_lazy_and_each_request_gets_a_distinct_private_pool(fake_transport):
    maker = factory(fake_transport)
    one = maker.open(deadline=145.0)
    two = maker.open(deadline=145.0)

    assert fake_transport.sockets == []
    assert one._pool is not two._pool and one.client is not two.client
    assert one._pool.max_connections == two._pool.max_connections == 1
    assert one._raw_client.connection_pool is one._pool
    assert two._raw_client.connection_pool is two._pool
    assert one.finish() is True and two.finish() is True


@pytest.mark.parametrize(
    "configuration",
    [
        {"REDIS_USE_SENTINEL": True},
        {"REDIS_USE_CLUSTERS": True},
        {"REDIS_ENABLE_CLIENT_SIDE_CACHE": True},
        {"REDIS_USE_SSL": True, "REDIS_SSL_CERT_REQS": "CERT_OPTIONAL"},
    ],
)
def test_rejected_transports_fail_before_allocating_a_socket(fake_transport, configuration):
    with pytest.raises(redis_runtime.CasdoorRedisUnavailable) as caught:
        factory(fake_transport, **configuration).open(deadline=145.0)
    assert str(caught.value) == "casdoor_redis_unavailable"
    assert caught.value.__context__ is None
    assert fake_transport.sockets == []


def test_timed_out_business_eval_has_one_wire_attempt_and_closes_once(fake_transport, monkeypatch):
    scope = factory(fake_transport).open(deadline=145.0)
    fake_transport.clock.value = 143.5  # 0.5 seconds remain after the cleanup reserve.
    wire = RespSocket(fake_transport.clock)
    wire.force_timeout_on = b"EVAL"

    def connect_with_wire(self):
        fake_transport.sockets.append(wire)
        return wire

    monkeypatch.setattr(Connection, "_connect", connect_with_wire)

    with pytest.raises(redis_runtime.CasdoorRedisUnavailable) as caught:
        scope.client.eval("return 1", 1, b"physical:already-prefixed")

    assert str(caught.value) == "casdoor_redis_unavailable"
    assert caught.value.__context__ is None
    assert [frame[0] for frame in wire.frames].count(b"EVAL") == 1
    assert wire.frames[-1][-1] == b"physical:already-prefixed"
    assert wire.deadlines and max(wire.deadlines) <= 0.5
    before_retry = list(wire.frames)
    with pytest.raises(redis_runtime.CasdoorRedisUnavailable):
        scope.client.eval("return 1", 1, b"physical:already-prefixed")
    assert wire.frames == before_retry
    assert scope.finish() is True
    assert scope.finish() is True and wire.close_calls == 1 and wire.is_closed


def test_unknown_local_close_state_denies_finish_idempotently(fake_transport, monkeypatch):
    scope = factory(fake_transport).open(deadline=145.0)
    wire = RespSocket(fake_transport.clock)
    wire.leave_open_on_close = True

    def connect_with_wire(self):
        fake_transport.sockets.append(wire)
        return wire

    monkeypatch.setattr(Connection, "_connect", connect_with_wire)
    scope.client.get("lazy-connect")

    assert scope.finish() is False
    assert scope.finish() is False
    assert wire.close_calls == 1 and wire.fileno() == 42
