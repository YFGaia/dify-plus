"""Installed redis-py pools, parsers, locks and scripts on an offline fake socket."""

import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID

import pytest
from core.casdoor import redis_runtime as runtime
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope
from redis.connection import Connection


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def settings(**changes):
    return SimpleNamespace(
        **(
            dict(
                REDIS_USE_SENTINEL=False,
                REDIS_USE_CLUSTERS=False,
                REDIS_ENABLE_CLIENT_SIDE_CACHE=False,
                REDIS_HOST="redis.invalid",
                REDIS_PORT=6379,
                REDIS_USERNAME="offline-user",
                REDIS_PASSWORD="offline-password",
                REDIS_DB=4,
                REDIS_KEY_PREFIX=" private ",
                REDIS_SERIALIZATION_PROTOCOL=3,
                REDIS_USE_SSL=False,
                REDIS_SSL_CERT_REQS="CERT_REQUIRED",
                REDIS_SSL_CA_CERTS=None,
                REDIS_SSL_CERTFILE=None,
                REDIS_SSL_KEYFILE=None,
            )
            | changes
        )
    )


class Socket:
    def __init__(self, clock):
        self.clock = clock
        self.commands = []
        self.timeouts = []
        self.buffer = b""
        self.closed = False
        self.loaded = False
        self.fail = None
        self.advance = 0
        self.close_count = 0

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)

    def gettimeout(self):
        return self.timeout

    def sendall(self, packed):
        parts = packed.split(b"\r\n")
        command = tuple(parts[2::2])
        self.commands.append(command)
        self.clock.now += self.advance
        if self.fail == command[0]:
            raise socket.timeout("private wire text")
        name = command[0]
        if name == b"HELLO":
            response = b"%1\r\n+proto\r\n:3\r\n"
        elif name in (b"CLIENT", b"SELECT", b"AUTH", b"SET"):
            response = b"+OK\r\n"
        elif name == b"SCRIPT":
            self.loaded = True
            response = b"$3\r\nsha\r\n"
        elif name == b"EVALSHA":
            response = b":1\r\n" if self.loaded else b"-NOSCRIPT missing\r\n"
        elif name == b"EVAL":
            response = b":1000\r\n"
        elif name == b"GET":
            response = b"$-1\r\n"
        else:
            response = b":1\r\n"
        self.buffer += response

    def recv(self, size):
        if not self.buffer:
            raise socket.timeout()
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def recv_into(self, buffer):
        chunk = self.recv(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True
        self.close_count += 1

    def fileno(self):
        return -1 if self.closed else 17


@pytest.fixture
def wire(monkeypatch):
    clock, sockets, connects = Clock(), [], []

    def connect(connection):
        connects.append(connection.socket_connect_timeout)
        sock = Socket(clock)
        sock.settimeout(connection.socket_timeout)
        sockets.append(sock)
        return sock

    monkeypatch.setattr(Connection, "_connect", connect)
    return SimpleNamespace(clock=clock, sockets=sockets, connects=connects)


def opened(wire, **changes):
    return runtime.CasdoorRedisRuntimeFactory(settings(**changes), clock=wire.clock).open(deadline=145.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"REDIS_USE_SENTINEL": True},
        {"REDIS_USE_CLUSTERS": True},
        {"REDIS_ENABLE_CLIENT_SIDE_CACHE": True},
        {"REDIS_HOST": "/tmp/redis.sock"},
        {"REDIS_URL": "unix:///redis"},
        {"REDIS_CONNECTION_CLASS": object},
        {"REDIS_SERIALIZATION_PROTOCOL": 4},
        {"REDIS_USE_SSL": True, "REDIS_SSL_CERT_REQS": "CERT_NONE"},
        {"REDIS_USE_SSL": True, "REDIS_SSL_CERT_REQS": "CERT_OPTIONAL"},
    ],
)
def test_unsupported_transport_fails_before_pool_or_socket(wire, changes):
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        opened(wire, **changes)
    assert not wire.sockets


def test_factory_lazy_private_pools_exact_protocol_and_finite_no_retry(wire):
    factory = runtime.CasdoorRedisRuntimeFactory(settings(), clock=wire.clock)
    assert not wire.sockets
    with ThreadPoolExecutor(max_workers=2) as executor:
        scopes = list(executor.map(lambda _: factory.open(deadline=145), range(2)))
    first, second = scopes
    assert first.client is not second.client and first._pool is not second._pool
    assert not wire.sockets
    config = first._pool.connection_kwargs
    assert config["protocol"] == 3 and config["db"] == 4
    assert config["username"] == "offline-user" and config["password"] == "offline-password"
    assert config["retry"].get_retries() == 0
    assert config["retry_on_error"] == [] and config["retry_on_timeout"] is False
    assert config["health_check_interval"] == 0
    assert first._pool.max_connections == 1 and first._pool.timeout == 5
    assert config["socket_timeout"] == config["socket_connect_timeout"] == 5
    assert first.finish() is second.finish() is True


def test_verified_tls_preserves_ca_client_cert_and_hostname(wire):
    scope = opened(
        wire,
        REDIS_USE_SSL=True,
        REDIS_SSL_CA_CERTS="offline-ca",
        REDIS_SSL_CERTFILE="offline-cert",
        REDIS_SSL_KEYFILE="offline-key",
    )
    config = scope._pool.connection_kwargs
    assert config["ssl_cert_reqs"] == ssl.CERT_REQUIRED and config["ssl_check_hostname"] is True
    assert (config["ssl_ca_certs"], config["ssl_certfile"], config["ssl_keyfile"]) == (
        "offline-ca",
        "offline-cert",
        "offline-key",
    )
    assert not wire.sockets and scope.finish()


@pytest.mark.parametrize("protocol", [2, 3])
def test_real_parser_exact_prefix_raw_eval_and_all_commands_suppressed(wire, monkeypatch, protocol):
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled

    observations = []
    original = Socket.sendall

    def send(sock, packed):
        observations.append(is_instrumentation_enabled())
        original(sock, packed)

    monkeypatch.setattr(Socket, "sendall", send)
    scope = opened(wire, REDIS_SERIALIZATION_PROTOCOL=protocol)
    assert scope.client.set("business", "value")
    scope.client.eval("return 1", 1, "private:physical")
    lock = scope.client.lock("lease", timeout=10, blocking=False, thread_local=False)
    assert lock.acquire(blocking=False, token=b"private-token")
    with scope.client._casdoor_cleanup_scope():
        lock.do_release(b"private-token")
    commands = wire.sockets[0].commands
    assert (b"SET", b"private:business", b"value") in commands
    assert (b"EVAL", b"return 1", b"1", b"private:physical") in commands
    assert lock.name == "private:lease"
    assert [c[0] for c in commands].count(b"EVALSHA") == 2
    assert any(c[:2] == (b"SCRIPT", b"LOAD") for c in commands)
    assert observations and not any(observations)
    assert scope.finish() is True and scope.finish() is True
    assert wire.sockets[0].close_count == 1


def test_actual_timeouts_shrink_and_cleanup_uses_reserved_remainder(wire):
    scope = opened(wire)
    wire.clock.now = 142
    scope.client.get("one")
    assert wire.connects == [2]
    wire.clock.now = 143
    scope.client.get("two")
    assert scope._pool.timeout == scope._pool._connections[0].socket_timeout == 1
    wire.clock.now = 144.2
    before = len(wire.sockets[0].commands)
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        scope.client.set("three", "blocked")
    assert len(wire.sockets[0].commands) == before
    with scope.client._casdoor_cleanup_scope():
        scope.client.eval("return 1", 1, "physical")
    assert scope._pool._connections[0].socket_timeout == pytest.approx(0.8)
    assert scope.finish()


def test_cleanup_timeout_is_false_and_reentry_never_retries_wire(wire):
    scope = opened(wire)
    leases = CasdoorLeases(scope.client, CasdoorLeaseScope(UUID(int=1), "subject"), deadline=145, monotonic=wire.clock)
    leases.acquire()
    wire.sockets[0].fail = b"EVALSHA"
    wire.clock.now = 144.2
    assert leases.release() is False
    calls = sum(len(sock.commands) for sock in wire.sockets)
    assert leases.release() is False
    assert sum(len(sock.commands) for sock in wire.sockets) == calls
    assert scope.finish() is True


def test_unknown_business_mutation_not_retried_and_error_has_no_raw_context(wire):
    scope = opened(wire)
    scope.client.get("initialize")
    wire.sockets[0].fail = b"SET"
    with pytest.raises(runtime.CasdoorRedisUnavailable) as caught:
        scope.client.set("one", "private")
    assert caught.value.__context__ is None and str(caught.value) == "casdoor_redis_unavailable"
    calls = len(wire.sockets[0].commands)
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        scope.client.set("one", "private")
    assert len(wire.sockets[0].commands) == calls and len(wire.sockets) == 1
    assert scope.finish()


def test_late_reply_and_late_close_fail_closed(wire):
    scope = opened(wire)
    scope.client.get("initialize")
    wire.sockets[0].advance = 45
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        scope.client.set("late", "value")
    assert scope.finish() is False
    assert scope.finish() is False
    assert all(sock.closed for sock in wire.sockets)


def test_finish_detects_retained_socket_and_is_idempotent(wire, monkeypatch):
    scope = opened(wire)
    scope.client.get("initialize")
    monkeypatch.setattr(wire.sockets[0], "close", lambda: None)
    assert scope.finish() is False and scope.finish() is False
    assert scope._pool._connections[0]._sock is None
    wire.sockets[0].closed = True


def test_pool_acquisition_wait_shrinks_without_extra_slot(wire, monkeypatch):
    from queue import Empty

    scope = opened(wire)
    held = scope._pool.pool.get_nowait()
    assert held is None
    waits = []

    def empty(*, block, timeout):
        waits.append(timeout)
        raise Empty

    monkeypatch.setattr(scope._pool.pool, "get", empty)
    wire.clock.now = 143.75
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        scope.client.get("never-sent")
    assert waits == [0.25] and scope._pool.pool.qsize() == 0 and not wire.sockets
    assert scope.finish()


def test_initialization_failure_never_reconnects(wire, monkeypatch):
    calls = []

    def failed(connection):
        calls.append(connection.socket_connect_timeout)
        raise socket.timeout("private initialization")

    monkeypatch.setattr(Connection, "_connect", failed)
    scope = opened(wire)
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        scope.client.get("unavailable")
    assert calls == [5] and len(scope._pool._connections) == 1
    assert scope.finish()


def test_partial_client_construction_still_disconnects_private_pool(wire, monkeypatch):
    pools, closes = [], []
    original = runtime._RequestPool.__init__
    disconnect = runtime._RequestPool.disconnect

    def initialize(self, **kwargs):
        original(self, **kwargs)
        pools.append(self)

    def close(self):
        closes.append(self)
        disconnect(self)

    def fail(self, **kwargs):
        raise ValueError("private constructor")

    monkeypatch.setattr(runtime._RequestPool, "__init__", initialize)
    monkeypatch.setattr(runtime._RequestPool, "disconnect", close)
    monkeypatch.setattr(runtime._RequestRedis, "__init__", fail)
    with pytest.raises(runtime.CasdoorRedisUnavailable) as caught:
        opened(wire)
    assert caught.value.__context__ is None
    assert len(pools) == 1 and closes == pools and not wire.sockets


def test_sensitive_context_teardown_failure_still_closes_constructed_pool(wire, monkeypatch):
    from contextlib import contextmanager

    pools = []
    original = runtime._RequestPool.__init__

    def initialize(self, **kwargs):
        original(self, **kwargs)
        pools.append(self)

    @contextmanager
    def failed_context():
        yield
        raise ValueError("private teardown")

    monkeypatch.setattr(runtime._RequestPool, "__init__", initialize)
    monkeypatch.setattr(runtime, "sensitive_redis_call", failed_context)
    with pytest.raises(runtime.CasdoorRedisUnavailable):
        opened(wire)
    assert len(pools) == 1 and pools[0]._request_budget.closed is True
    assert not wire.sockets


def test_close_exception_and_control_signal_return_false_once(wire, monkeypatch):
    scope = opened(wire)
    scope.client.get("initialize")
    closes = []

    def close():
        closes.append(True)
        raise KeyboardInterrupt()

    monkeypatch.setattr(scope._raw_client, "close", close)
    assert scope.finish() is False and scope.finish() is False
    assert closes == [True] and wire.sockets[0].closed
