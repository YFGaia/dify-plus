"""Real redis-py transport/encoding with offline sockets; no live Redis claim."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis import Redis
from redis._parsers import _RESP2Parser
from redis.backoff import NoBackoff
from redis.cluster import PRIMARY, ClusterNode, RedisCluster
from redis.connection import Connection, ConnectionPool, SSLConnection
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry
from redis.sentinel import Sentinel, SentinelConnectionPool

from extensions.ext_redis import RedisClientWrapper
from services import invitation_issuance_service_extend as svc

TOKEN = "00000000-0000-4000-8000-000000000301"
PAYLOAD = b'{"exact":"invitation bytes"}'


def resp(value):
    if value is None:
        return b"$-1\r\n"
    if type(value) is int:
        return b":" + str(value).encode() + b"\r\n"
    if type(value) is list:
        return b"*" + str(len(value)).encode() + b"\r\n" + b"".join(resp(item) for item in value)
    if type(value) is bytes:
        return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"
    raise AssertionError("unsupported fake reply")


def pop_frame(buffer):
    """Parse complete RESP arrays, retaining partial writes for the next chunk."""
    if b"\r\n" not in buffer:
        return None
    first, _ = buffer.split(b"\r\n", 1)
    assert first.startswith(b"*")
    count, offset, args = int(first[1:]), len(first) + 2, []
    for _ in range(count):
        end = buffer.find(b"\r\n", offset)
        if end < 0:
            return None
        assert buffer[offset : offset + 1] == b"$"
        size = int(buffer[offset + 1 : end])
        offset = end + 2
        if len(buffer) < offset + size + 2:
            return None
        args.append(bytes(buffer[offset : offset + size]))
        offset += size + 2
    del buffer[:offset]
    return args


class Wire:
    def __init__(self, *, eval_mode="published", readback="exact"):
        self.eval_mode, self.readback = eval_mode, readback
        self.frames, self.sockets, self.releases = [], [], []
        self.value, self.ttl = None, -2
        self.business = False
        self.setup_retries = 0
        self.send_chunks = 0
        self.eval_responses = 0

    def socket(self):
        result = FakeSocket(self)
        self.sockets.append(result)
        return result

    def answer(self, sock, frame):
        self.frames.append(frame)
        name = frame[0].upper()
        if name in (b"CLIENT", b"AUTH", b"SELECT", b"PING", b"READONLY"):
            return b"+OK\r\n"
        self.business = True
        if name == b"EVAL":
            self.eval_responses += 1
            if self.eval_mode == "conflict":
                return resp(b"conflict")
            if self.eval_mode in ("MOVED", "ASK", "TRYAGAIN", "READONLY"):
                detail = "1 127.0.0.1:6379" if self.eval_mode in ("MOVED", "ASK") else "unavailable"
                return f"-{self.eval_mode} {detail}\r\n".encode()
            self.value, self.ttl = frame[4], 1234
            if self.eval_mode == "malformed":
                return resp(1)
            if self.eval_mode in ("interrupt", "exit"):
                sock.read_failure = (
                    KeyboardInterrupt("raw private socket content") if self.eval_mode == "interrupt" else SystemExit(73)
                )
                return b""
            if self.eval_mode == "lost_ack" and self.eval_responses == 1:
                sock.read_failure = TimeoutError("raw private socket content")
                return b""
            return resp(b"published")
        if name == b"MULTI":
            return resp(b"BAD" if self.readback == "bad_ok" else b"OK")
        if name in (b"GET", b"PTTL"):
            return resp(b"BAD" if self.readback == "bad_queued" else b"QUEUED")
        assert name == b"EXEC"
        values = {
            "exact": [self.value, self.ttl],
            "consumed": [None, -2],
            "expired": [self.value, 0],
            "persistent": [self.value, -1],
            "mismatch": [b"different", 100],
            "null": None,
            "malformed": [self.value],
            "ttl_bytes": [self.value, b"100"],
            "bad_ok": [self.value, self.ttl],
            "bad_queued": [self.value, self.ttl],
        }
        if self.readback == "redirect":
            return b"-MOVED 1 127.0.0.1:6379\r\n"
        if self.readback in ("interrupt", "exit"):
            sock.read_failure = (
                KeyboardInterrupt("raw private socket content") if self.readback == "interrupt" else SystemExit(73)
            )
            return b""
        return resp(values[self.readback])

    def business_frames(self):
        return [frame for frame in self.frames if frame[0] in (b"EVAL", b"MULTI", b"GET", b"PTTL", b"EXEC")]


class FakeSocket:
    def __init__(self, wire):
        self.wire, self.input, self.output = wire, bytearray(), bytearray()
        self.timeout, self.read_failure, self.closed = 1, None, False

    def sendall(self, data):
        self.wire.send_chunks += 1
        if self.wire.eval_mode == "partial" and b"EVAL" in data:
            self.input.extend(bytes(data)[:20])
            raise TimeoutError("raw private socket content")
        self.input.extend(data)
        while (frame := pop_frame(self.input)) is not None:
            self.output.extend(self.wire.answer(self, frame))

    def recv(self, count):
        if self.read_failure:
            failure, self.read_failure = self.read_failure, None
            raise failure
        if not self.output:
            raise TimeoutError()
        result = bytes(self.output[:count])
        del self.output[:count]
        return result

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True
        self.wire.business = False


def client_for(wire, monkeypatch, *, topology="standalone", trap_retry=True):
    monkeypatch.setattr(Connection, "_connect", lambda _: wire.socket())
    monkeypatch.setattr(SSLConnection, "_connect", lambda _: wire.socket())
    retry = Retry(NoBackoff(), 2, supported_errors=(RedisTimeoutError,))
    kwargs = dict(parser_class=_RESP2Parser, retry=retry, socket_timeout=1, decode_responses=True)
    if topology.startswith("sentinel"):
        sentinel = Sentinel([])
        monkeypatch.setattr(sentinel, "discover_master", lambda _: ("offline", 6379))
        pool = SentinelConnectionPool("offline", sentinel, ssl=topology == "sentinel_ssl", **kwargs)
    else:
        pool = ConnectionPool(connection_class=SSLConnection if topology == "ssl" else Connection, **kwargs)
    raw = Redis(connection_pool=pool)
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    real_release = ConnectionPool.release

    def release(supplied_pool, connection):
        wire.releases.append(connection._sock is None)
        return real_release(supplied_pool, connection)

    monkeypatch.setattr(ConnectionPool, "release", release)
    real_retry = Retry.call_with_retry

    def trap(self, *args, **kw):
        if wire.business and trap_retry:
            raise AssertionError("business-window retry callback invoked")
        wire.setup_retries += 1
        return real_retry(self, *args, **kw)

    monkeypatch.setattr(Retry, "call_with_retry", trap)
    if topology == "cluster":
        cluster = RedisCluster.__new__(RedisCluster)
        node = ClusterNode("offline", 6379, server_type=PRIMARY)
        node.redis_connection = raw
        cluster.user_on_connect_func = None
        cluster.encoder = pool.get_encoder()
        cluster.nodes_manager = SimpleNamespace(slots_cache={})
        cluster.nodes_manager.slots_cache[cluster.keyslot(b"one:member_invite:token:" + TOKEN.encode())] = [node]
        wrapper._client = cluster
    return wrapper, raw, pool


@pytest.fixture(autouse=True)
def prefix(config_overrides):
    config_overrides(REDIS_KEY_PREFIX=" one ")


@pytest.mark.parametrize("topology", ["standalone", "ssl", "sentinel", "sentinel_ssl", "cluster"])
def test_real_connections_publish_single_frame_without_command_retry(monkeypatch, topology):
    wire = Wire()
    wrapper, _, _ = client_for(wire, monkeypatch, topology=topology)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=3600)
        is svc.PublicationOutcome.PUBLISHED
    )
    frames = wire.business_frames()
    assert len(frames) == 1 and frames[0] == [
        b"EVAL",
        svc._PUBLICATION_SCRIPT.encode(),
        b"1",
        b"one:member_invite:token:" + TOKEN.encode(),
        PAYLOAD,
        b"3600",
    ]
    assert wire.setup_retries > 0 and wire.releases == [True]
    assert all(sock.closed for sock in wire.sockets)


@pytest.mark.parametrize("mode", ["lost_ack", "malformed"])
def test_unknown_single_eval_confirms_only_exact_live_byte_transaction(monkeypatch, config_overrides, mode):
    wire = Wire(eval_mode=mode)
    wrapper, _, _ = client_for(wire, monkeypatch)
    old_answer = wire.answer

    def change_prefix(sock, frame):
        result = old_answer(sock, frame)
        if frame[0] == b"EVAL":
            config_overrides(REDIS_KEY_PREFIX="changed")
        return result

    wire.answer = change_prefix
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=3600)
        is svc.PublicationOutcome.PUBLISHED
    )
    frames = wire.business_frames()
    assert [frame[0] for frame in frames] == [b"EVAL", b"MULTI", b"GET", b"PTTL", b"EXEC"]
    assert frames[0][3] == frames[2][1] == frames[3][1] == b"one:member_invite:token:" + TOKEN.encode()
    assert wire.releases == [True, True] and len(wire.sockets) == 2


@pytest.mark.parametrize(
    "readback",
    [
        "consumed",
        "expired",
        "persistent",
        "mismatch",
        "null",
        "malformed",
        "ttl_bytes",
        "bad_ok",
        "bad_queued",
        "redirect",
    ],
)
def test_uncertain_readback_never_repeats_eval_or_confirms(monkeypatch, readback):
    wire = Wire(eval_mode="lost_ack", readback=readback)
    wrapper, _, _ = client_for(wire, monkeypatch)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1) is svc.PublicationOutcome.UNKNOWN
    )
    assert [frame[0] for frame in wire.business_frames()] == [b"EVAL", b"MULTI", b"GET", b"PTTL", b"EXEC"]
    assert wire.releases == [True, True]


@pytest.mark.parametrize("mode", ["pre_send", "partial", "MOVED", "ASK", "TRYAGAIN", "READONLY"])
def test_transport_or_server_error_has_no_business_resend(monkeypatch, mode):
    wire = Wire(eval_mode=mode)
    wrapper, _, _ = client_for(wire, monkeypatch)
    if mode == "pre_send":
        original = Connection.pack_command

        def fail_before_send(self, *args):
            if args[0] == "EVAL":
                raise RuntimeError("pre-send packing failure")
            return original(self, *args)

        monkeypatch.setattr(Connection, "pack_command", fail_before_send)
    result = svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1)
    assert result is svc.PublicationOutcome.UNKNOWN
    frames = wire.business_frames()
    assert sum(frame[0] == b"EVAL" for frame in frames) == (0 if mode in ("partial", "pre_send") else 1)
    if mode == "pre_send":
        assert frames == [] and wire.releases == [True]
    else:
        assert [frame[0] for frame in frames[-4:]] == [b"MULTI", b"GET", b"PTTL", b"EXEC"]
        assert wire.releases == [True, True]


def test_conflict_never_reads_or_republishes(monkeypatch):
    wire = Wire(eval_mode="conflict")
    wrapper, _, _ = client_for(wire, monkeypatch)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1) is svc.PublicationOutcome.CONFLICT
    )
    assert [frame[0] for frame in wire.business_frames()] == [b"EVAL"]


@pytest.mark.parametrize(
    "mode", ["cache", "custom_connection", "custom_pool", "callback", "packer", "replica", "client"]
)
def test_unsupported_modes_fail_before_any_eval(monkeypatch, mode):
    wire = Wire()
    wrapper, _, pool = client_for(wire, monkeypatch, topology="sentinel" if mode == "replica" else "standalone")
    if mode == "cache":
        pool.cache = object()
    elif mode == "custom_connection":
        pool.connection_class = type("CustomConnection", (Connection,), {})
    elif mode == "custom_pool":
        pool.__class__ = type("CustomPool", (ConnectionPool,), {})
    elif mode == "callback":
        pool.connection_kwargs["redis_connect_func"] = lambda _: None
    elif mode == "packer":
        pool.connection_kwargs["command_packer"] = object()
    elif mode == "replica":
        pool.is_master = False
    else:
        wrapper._client = object()
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1)
        is svc.PublicationOutcome.UNSUPPORTED
    )
    assert wire.business_frames() == []


@pytest.mark.parametrize("mode", ["release", "disconnect", "telemetry"])
def test_success_with_uncertain_teardown_is_unknown_and_never_reissued(monkeypatch, mode):
    wire = Wire()
    wrapper, _, _ = client_for(wire, monkeypatch)
    if mode == "release":
        real = ConnectionPool.release

        def fail_release(self, connection):
            real(self, connection)
            raise RuntimeError("raw private teardown")

        monkeypatch.setattr(ConnectionPool, "release", fail_release)
    elif mode == "disconnect":
        real = Connection.disconnect

        def fail_disconnect(self, *args, **kwargs):
            real(self, *args, **kwargs)
            if wire.business_frames():
                raise RuntimeError("raw private teardown")

        monkeypatch.setattr(Connection, "disconnect", fail_disconnect)
    else:
        real = svc.sensitive_redis_call

        @contextmanager
        def fail_teardown():
            with real():
                yield
            if wire.business_frames():
                raise RuntimeError("raw private teardown")

        monkeypatch.setattr(svc, "sensitive_redis_call", fail_teardown)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1) is svc.PublicationOutcome.UNKNOWN
    )
    assert [frame[0] for frame in wire.business_frames()] == [b"EVAL"]
    assert all(sock.closed for sock in wire.sockets)
    assert wire.releases == [True]


def test_stock_client_command_can_retry_but_publisher_does_not(monkeypatch):
    wire = Wire(eval_mode="lost_ack")
    _, raw, _ = client_for(wire, monkeypatch, trap_retry=False)
    assert raw.eval(svc._PUBLICATION_SCRIPT, 1, "legacy-proof", PAYLOAD, 1) == "published"
    assert sum(frame[0] == b"EVAL" for frame in wire.business_frames()) == 2


def test_large_payload_counts_complete_resp_frame_not_socket_chunks(monkeypatch):
    wire = Wire()
    wrapper, _, pool = client_for(wire, monkeypatch)
    # The actual Python serializer splits a large bulk argument into several writes.
    from redis.connection import PythonRespSerializer

    pool.connection_kwargs["command_packer"] = PythonRespSerializer(100, pool.get_encoder().encode)
    # Custom packers are correctly rejected; construct an ordinary connection and
    # set the standard serializer cutoff without replacing any transport method.
    pool.connection_kwargs.pop("command_packer")
    connection = pool.make_connection()
    connection._command_packer = PythonRespSerializer(100, connection.encoder.encode)
    pool._available_connections.append(connection)
    payload = b"x" * 7000
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=payload, ttl=1)
        is svc.PublicationOutcome.PUBLISHED
    )
    assert len(wire.business_frames()) == 1 and wire.send_chunks > 1


@pytest.mark.parametrize("failure", ["acquire", "review", "pack"])
def test_failure_before_eval_attempt_emits_no_business_frame_or_readback(monkeypatch, failure):
    wire = Wire()
    wrapper, _, _ = client_for(wire, monkeypatch)
    if failure == "acquire":
        monkeypatch.setattr(ConnectionPool, "get_connection", Mock(side_effect=RuntimeError("acquire failure")))
    elif failure == "review":
        monkeypatch.setattr(svc, "_reviewed_connection", Mock(side_effect=RuntimeError("review failure")))
    else:
        original = Connection.pack_command

        def fail_pack(self, *args):
            if args[0] == "EVAL":
                raise RuntimeError("pack failure")
            return original(self, *args)

        monkeypatch.setattr(Connection, "pack_command", fail_pack)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1) is svc.PublicationOutcome.UNKNOWN
    )
    assert wire.business_frames() == []


def test_failed_disconnect_before_socket_clear_attempts_release_without_reusing_dirty_connection(monkeypatch):
    wire = Wire()
    wrapper, _, pool = client_for(wire, monkeypatch)
    original = Connection.disconnect
    captured = []

    def fail_disconnect(self, *args, **kwargs):
        if wire.business_frames():
            captured.append(self)
            raise RuntimeError("disconnect before socket clear")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Connection, "disconnect", fail_disconnect)
    assert (
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1) is svc.PublicationOutcome.UNKNOWN
    )
    assert wire.releases == [False]
    assert not pool._available_connections and not pool._in_use_connections
    assert len(captured) == 2  # Explicit disconnect, then guarded release disconnect.
    assert [frame[0] for frame in wire.business_frames()] == [b"EVAL"]
    monkeypatch.setattr(Connection, "disconnect", original)
    original(captured[0])


@pytest.mark.parametrize("signal", ["interrupt", "exit"])
@pytest.mark.parametrize("stage", ["eval", "readback"])
def test_interrupt_after_eval_cleans_up_and_propagates_without_further_work(monkeypatch, signal, stage):
    wire = Wire(
        eval_mode=signal if stage == "eval" else "lost_ack", readback=signal if stage == "readback" else "exact"
    )
    wrapper, _, _ = client_for(wire, monkeypatch)
    expected = KeyboardInterrupt if signal == "interrupt" else SystemExit
    with pytest.raises(expected) as caught:
        svc.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=1)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert str(caught.value) == ("invitation publication interrupted" if signal == "interrupt" else "1")
    frames = [frame[0] for frame in wire.business_frames()]
    assert frames == ([b"EVAL"] if stage == "eval" else [b"EVAL", b"MULTI", b"GET", b"PTTL", b"EXEC"])
    assert wire.releases == ([True] if stage == "eval" else [True, True])
    assert all(sock.closed for sock in wire.sockets)
