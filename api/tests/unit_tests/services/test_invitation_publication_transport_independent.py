"""Independent checks of the frozen invitation issuer boundary."""

import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from redis import Redis
from redis._parsers import _RESP2Parser
from redis.backoff import NoBackoff
from redis.connection import Connection, ConnectionPool
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from extensions.ext_redis import RedisClientWrapper
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository
from services import invitation_issuance_service_extend as issuer

TOKEN = "00000000-0000-4000-8000-000000000391"
PAYLOAD = b'{"invitation":"independent-p3i"}'


def _bulk(value):
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


def _command(buffer):
    marker = buffer.find(b"\r\n")
    if marker < 0 or buffer[:1] != b"*":
        return None
    size = int(buffer[1:marker])
    offset = marker + 2
    values = []
    for _ in range(size):
        end = buffer.find(b"\r\n", offset)
        if end < 0 or buffer[offset : offset + 1] != b"$":
            return None
        length = int(buffer[offset + 1 : end])
        start = end + 2
        finish = start + length
        if len(buffer) < finish + 2:
            return None
        values.append(bytes(buffer[start:finish]))
        offset = finish + 2
    del buffer[:offset]
    return values


class _OfflineRedis:
    """A RESP peer that records complete command frames and serves readback."""

    def __init__(self, *, malformed_eval=False):
        self.malformed_eval = malformed_eval
        self.frames = []
        self.connections = []
        self.value = None
        self.in_business = False

    def socket(self):
        sock = _Socket(self)
        self.connections.append(sock)
        return sock

    def answer(self, sock, frame):
        self.frames.append(frame)
        command = frame[0].upper()
        if command == b"PING":
            return b"+PONG\r\n"
        if command == b"CLIENT":
            return b"+OK\r\n"
        if command == b"EVAL":
            self.value = frame[4]
            self.in_business = True
            if self.malformed_eval:
                return b":1\r\n"
            return _bulk(b"published")
        if command == b"MULTI":
            return b"+OK\r\n"
        if command in (b"GET", b"PTTL"):
            return b"+QUEUED\r\n"
        if command == b"EXEC":
            return b"*2\r\n" + _bulk(self.value) + b":321\r\n"
        raise AssertionError("unexpected command in offline protocol peer")


class _Socket:
    def __init__(self, peer):
        self.peer = peer
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.timeout = 1
        self.closed = False

    def sendall(self, data):
        self.incoming.extend(data)
        while frame := _command(self.incoming):
            self.outgoing.extend(self.peer.answer(self, frame))

    def recv(self, size):
        if not self.outgoing:
            raise TimeoutError("offline peer has no response")
        result = bytes(self.outgoing[:size])
        del self.outgoing[:size]
        return result

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def shutdown(self, _how):
        return None

    def close(self):
        self.closed = True
        self.peer.in_business = False


def _publisher(monkeypatch, peer, *, retry_trap=True):
    monkeypatch.setattr(Connection, "_connect", lambda _connection: peer.socket())
    pool = ConnectionPool(
        connection_class=Connection,
        parser_class=_RESP2Parser,
        retry=Retry(NoBackoff(), 2, supported_errors=(RedisTimeoutError,)),
        socket_timeout=1,
    )
    raw = Redis(connection_pool=pool)
    wrapper = RedisClientWrapper()
    wrapper.initialize(raw)
    original_retry = Retry.call_with_retry

    def guarded_retry(self, *args, **kwargs):
        if retry_trap and peer.in_business:
            raise AssertionError("redis-py retry path entered after EVAL send began")
        return original_retry(self, *args, **kwargs)

    monkeypatch.setattr(Retry, "call_with_retry", guarded_retry)
    return wrapper


@pytest.mark.parametrize("malformed_eval", [False, True])
def test_actual_redis_74_transport_sends_one_eval_without_command_retry(monkeypatch, config_overrides, malformed_eval):
    config_overrides(REDIS_KEY_PREFIX=" independent ")
    peer = _OfflineRedis(malformed_eval=malformed_eval)
    wrapper = _publisher(monkeypatch, peer)

    result = issuer.InvitationPublisher(wrapper).publish(token=TOKEN, payload=PAYLOAD, ttl=3600)

    assert result is issuer.PublicationOutcome.PUBLISHED
    evals = [frame for frame in peer.frames if frame[0].upper() == b"EVAL"]
    assert len(evals) == 1
    assert evals[0] == [
        b"EVAL",
        issuer._PUBLICATION_SCRIPT.encode(),
        b"1",
        b"independent:member_invite:token:" + TOKEN.encode(),
        PAYLOAD,
        b"3600",
    ]
    expected_commands = [b"EVAL"]
    if malformed_eval:
        expected_commands.extend([b"MULTI", b"GET", b"PTTL", b"EXEC"])
    assert [
        frame[0].upper() for frame in peer.frames if frame[0].upper() in {b"EVAL", b"MULTI", b"GET", b"PTTL", b"EXEC"}
    ] == expected_commands
    assert all(sock.closed for sock in peer.connections)


def _withdrawn_session(session, *, current_join):
    workspace = Tenant(name="Independent invitation workspace")
    account = Account(name="Independent invite target", email="independent-target@example.test")
    session.add_all([workspace, account])
    session.flush()
    if current_join:
        session.add(TenantAccountJoin(tenant_id=workspace.id, account_id=account.id, role=TenantAccountRole.NORMAL))
    repository = InvitationAuthorityRepository()
    lifecycle = repository.set_lifecycle_state(
        session, account_id=account.id, workspace_id=workspace.id, state="withdrawn"
    )
    row = session.get(InvitationAuthorityLifecycleExtend, lifecycle.lifecycle_id)
    row.epoch = 9
    session.commit()
    return SimpleNamespace(workspace=workspace, account=account, repository=repository, lifecycle_id=row.lifecycle_id)


def test_withdrawn_without_join_reopens_once_before_publication(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = _withdrawn_session(session, current_join=False)
        observed = []

        def publish(_self, *, token, payload, ttl):
            data = json.loads(payload)
            with sqlite_session_factory() as observer:
                row = observer.get(InvitationAuthorityLifecycleExtend, state.lifecycle_id)
                records = list(observer.scalars(sa.select(InvitationAuthorityIssuanceExtend)))
                assert row.state == "active" and row.epoch == 10
                assert len(records) == 1 and records[0].payload_json.encode() == payload
            assert data["invitation_authority"]["join_id_at_issue"] is None
            assert data["invitation_authority"]["lifecycle_epoch"] == 10
            assert data["invitation_authority"]["token_digest"] == sha256(token.encode()).hexdigest()
            assert UUID(token).version == 4 and ttl > 0
            observed.append(True)
            return issuer.PublicationOutcome.PUBLISHED

        monkeypatch.setattr(issuer.InvitationPublisher, "publish", publish)
        token = issuer.InvitationIssuer.issue(
            state.workspace,
            state.account,
            "normal",
            requires_setup=False,
            actor_id=state.account.id,
            session=session,
            redis=Mock(),
        )
        assert UUID(token).version == 4 and observed == [True]


def test_withdrawn_with_join_fails_closed_without_publisher(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = _withdrawn_session(session, current_join=True)
        publisher = Mock()
        monkeypatch.setattr(issuer.InvitationPublisher, "publish", publisher)
        with pytest.raises(issuer.InvitationIssuanceError, match="invitation_issuance_unconfirmed"):
            issuer.InvitationIssuer.issue(
                state.workspace,
                state.account,
                "normal",
                requires_setup=False,
                actor_id=state.account.id,
                session=session,
                redis=Mock(),
            )
        publisher.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            row = observer.get(InvitationAuthorityLifecycleExtend, state.lifecycle_id)
            assert row.state == "withdrawn" and row.epoch == 9
            assert list(observer.scalars(sa.select(InvitationAuthorityIssuanceExtend))) == []
