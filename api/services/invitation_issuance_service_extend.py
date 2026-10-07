"""SQL-first invitation issuance and one-shot Redis publication.

Only the already-authorized invitation service may call the issuer. It commits
an immutable SQL snapshot before publication; failed or uncertain publication
never returns a token. Raw transport deliberately bypasses redis-py command
retry/redirect machinery. Readback proves current bytes, not consumption.
"""

import json
from enum import StrEnum
from hashlib import sha256
from importlib.metadata import version
from typing import Any
from uuid import uuid4

from redis import Redis
from redis._parsers import _HiredisParser, _RESP2Parser, _RESP3Parser
from redis.cluster import PRIMARY, RedisCluster
from redis.connection import Connection, ConnectionPool, HiredisRespSerializer, PythonRespSerializer, SSLConnection
from redis.sentinel import SentinelConnectionPool, SentinelManagedConnection, SentinelManagedSSLConnection
from sqlalchemy import select
from sqlalchemy.orm import Session

from configs import dify_config
from extensions.ext_redis import RedisClientWrapper
from extensions.redis_names import serialize_redis_name
from libs.sensitive_redis import sensitive_redis_call
from models.account import Account, Tenant, TenantAccountJoin
from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository

# Keep expiration within a positive signed 32-bit number of seconds. No expiry
# coercion, infinite token or zero-second publication is permitted.
MAX_INVITATION_TTL_SECONDS = 2**31 - 1
_PUBLICATION_SCRIPT = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
    return 'published'
end
return 'conflict'
"""


class InvitationIssuanceError(Exception):
    """Fixed outcome only; no Redis/SQL exception details or invitation content."""


class PublicationOutcome(StrEnum):
    PUBLISHED = "published"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


def _require_reviewed(condition: bool) -> None:
    if not condition:
        raise InvitationIssuanceError("invitation_transport_unsupported")


def _connection_callback_is_reviewed(callback: Any, cluster: RedisCluster | None) -> bool:
    return callback is None or (
        cluster is not None
        and getattr(callback, "__self__", None) is cluster
        and getattr(callback, "__func__", None) is RedisCluster.on_connect
        and cluster.user_on_connect_func is None
    )


def _reviewed_pool(client: Redis | RedisCluster, key: bytes) -> tuple[ConnectionPool, RedisCluster | None]:
    _require_reviewed(version("redis") == "7.4.0")
    cluster = None
    if type(client) is RedisCluster:
        cluster = client
        _require_reviewed(cluster.user_on_connect_func is None)
        _require_reviewed(not {"get_node_from_key", "get_redis_connection", "on_connect"}.intersection(vars(cluster)))
        node = cluster.get_node_from_key(key, replica=False)
        _require_reviewed(node is not None and node.server_type == PRIMARY)
        client = cluster.get_redis_connection(node)
    _require_reviewed(type(client) is Redis and not client._single_connection_client)
    pool = client.connection_pool
    _require_reviewed(type(pool) in (ConnectionPool, SentinelConnectionPool))
    _require_reviewed(not {"get_connection", "make_connection", "release"}.intersection(vars(pool)))
    _require_reviewed(pool.cache is None and pool._cache_factory is None and not pool.maint_notifications_enabled())
    if type(pool) is SentinelConnectionPool:
        _require_reviewed(pool.is_master is True)
        _require_reviewed(pool.connection_class in (SentinelManagedConnection, SentinelManagedSSLConnection))
    else:
        _require_reviewed(pool.connection_class in (Connection, SSLConnection))
    kwargs = pool.connection_kwargs
    _require_reviewed(kwargs.get("command_packer") is None and kwargs.get("credential_provider") is None)
    _require_reviewed(kwargs.get("parser_class", _RESP2Parser) in (_RESP2Parser, _RESP3Parser, _HiredisParser))
    _require_reviewed(_connection_callback_is_reviewed(kwargs.get("redis_connect_func"), cluster))
    _require_reviewed(
        kwargs.get("encoding", "utf-8") == "utf-8" and kwargs.get("encoding_errors", "strict") == "strict"
    )
    return pool, cluster


def _reviewed_connection(connection: Connection, pool: ConnectionPool, cluster: RedisCluster | None) -> None:
    _require_reviewed(type(connection) is pool.connection_class)
    _require_reviewed(
        not {
            "connect",
            "connect_check_health",
            "send_packed_command",
            "read_response",
            "pack_command",
            "disconnect",
        }.intersection(vars(connection))
    )
    _require_reviewed(_connection_callback_is_reviewed(connection.redis_connect_func, cluster))
    _require_reviewed(not connection._connect_callbacks)
    _require_reviewed(type(connection._parser) in (_RESP2Parser, _RESP3Parser, _HiredisParser))
    _require_reviewed(type(connection._command_packer) in (PythonRespSerializer, HiredisRespSerializer))
    _require_reviewed(connection.encoder.encoding == "utf-8" and connection.encoder.encoding_errors == "strict")
    _require_reviewed(connection._sock is not None)


class InvitationPublisher:
    """Publish once, with at most one fresh-lease read-only reconciliation."""

    def __init__(self, redis: RedisClientWrapper):
        self._redis = redis

    @staticmethod
    def _exchange(
        pool: ConnectionPool,
        cluster: RedisCluster | None,
        *,
        key: bytes,
        payload: bytes,
        ttl: int,
        readback: bool,
    ) -> tuple[PublicationOutcome, bool]:
        connection = None
        clean = False
        body_finished = False
        eval_attempted = False
        signal = None
        outcome = PublicationOutcome.UNKNOWN
        try:
            with sensitive_redis_call():
                try:
                    connection = pool.get_connection()
                    _reviewed_connection(connection, pool, cluster)
                    if readback:
                        commands = (("MULTI",), ("GET", key), ("PTTL", key), ("EXEC",))
                    else:
                        commands = (("EVAL", _PUBLICATION_SCRIPT, 1, key, payload, ttl),)
                    replies = []
                    for command in commands:
                        # A dropped connection must not reconnect in the business
                        # window, where only these exact commands are permitted.
                        _require_reviewed(connection._sock is not None)
                        packed = connection.pack_command(*command)
                        if not readback:
                            eval_attempted = True
                        connection.send_packed_command(packed, check_health=False)
                        replies.append(connection.read_response(disable_decoding=True, disconnect_on_error=True))
                    if readback:
                        result = replies[3]
                        if (
                            replies[:3] == [b"OK", b"QUEUED", b"QUEUED"]
                            and type(result) is list
                            and len(result) == 2
                            and type(result[0]) is bytes
                            and result[0] == payload
                            and type(result[1]) is int
                            and result[1] > 0
                        ):
                            outcome = PublicationOutcome.PUBLISHED
                    elif type(replies[0]) is bytes:
                        if replies[0] == b"published":
                            outcome = PublicationOutcome.PUBLISHED
                        elif replies[0] == b"conflict":
                            outcome = PublicationOutcome.CONFLICT
                    body_finished = True
                except KeyboardInterrupt:
                    signal = "interrupt"
                    raise
                except SystemExit:
                    signal = "exit"
                    raise
                finally:
                    # Always attempt release, including after failed disconnect.
                    # In redis-py 7.4.0, mark-for-reconnect makes release disconnect
                    # before appending to idle; another failure leaves it out of
                    # idle after removing the lease from the in-use set.
                    if connection is not None:
                        try:
                            connection.disconnect()
                            _require_reviewed(connection._sock is None)
                        except BaseException:
                            connection.mark_for_reconnect()
                            raise
                        finally:
                            pool.release(connection)
                    clean = True
            return outcome, clean
        except KeyboardInterrupt:
            signal = signal or "interrupt"
        except SystemExit:
            signal = signal or "exit"
        except Exception:
            # Leave the handler and telemetry context before exposing an outcome.
            # A cleanup or context-exit failure cannot be upgraded by readback.
            pass
        del connection, key, payload
        if signal == "interrupt":
            raise KeyboardInterrupt("invitation publication interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        return PublicationOutcome.UNKNOWN, eval_attempted and clean and not body_finished

    def publish(self, *, token: str, payload: bytes, ttl: int) -> PublicationOutcome:
        pool = None
        cluster = None
        key = b""
        signal = None
        try:
            with sensitive_redis_call():
                prefix = self._redis._get_prefix()
                key = serialize_redis_name(f"member_invite:token:{token}", prefix).encode("utf-8")
                pool, cluster = _reviewed_pool(self._redis._require_client(), key)
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except Exception:
            pass
        else:
            outcome, may_reconcile = self._exchange(pool, cluster, key=key, payload=payload, ttl=ttl, readback=False)
            if outcome is PublicationOutcome.UNKNOWN and may_reconcile:
                outcome, _ = self._exchange(pool, cluster, key=key, payload=payload, ttl=ttl, readback=True)
            return outcome
        del key, payload, token
        if signal == "interrupt":
            raise KeyboardInterrupt("invitation publication interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        return PublicationOutcome.UNKNOWN if pool is not None else PublicationOutcome.UNSUPPORTED


class InvitationIssuer:
    """Called after existing invitation policy/member/RBAC work, never instead of it."""

    @staticmethod
    def issue(
        tenant: Tenant,
        account: Account,
        role: str,
        *,
        requires_setup: bool,
        actor_id: str,
        session: Session,
        redis: RedisClientWrapper,
    ) -> str:
        hours = dify_config.INVITE_EXPIRY_HOURS
        if type(hours) is not int or not 1 <= hours <= MAX_INVITATION_TTL_SECONDS // 3600:
            raise InvitationIssuanceError("invitation_expiry_invalid")
        token = str(uuid4())
        issuance_id = str(uuid4())
        payload = b""
        stored = False
        signal = None
        try:
            repository = InvitationAuthorityRepository()
            join = session.scalar(
                select(TenantAccountJoin)
                .where(TenantAccountJoin.account_id == account.id, TenantAccountJoin.tenant_id == tenant.id)
                .execution_options(populate_existing=True)
            )
            lifecycle = repository.get_lifecycle(session, account_id=account.id, workspace_id=tenant.id)
            if lifecycle is None or (lifecycle.state == "withdrawn" and join is None):
                lifecycle = repository.set_lifecycle_state(
                    session, account_id=account.id, workspace_id=tenant.id, state="active"
                )
            if lifecycle.state != "active":
                raise InvitationIssuanceError("invitation_lifecycle_conflict")
            payload_json = json.dumps(
                {
                    "account_id": account.id,
                    "email": account.email,
                    "workspace_id": tenant.id,
                    "role": str(role),
                    "requires_setup": requires_setup,
                    "invitation_authority": {
                        "schema_version": 1,
                        "issuance_id": issuance_id,
                        "lifecycle_id": lifecycle.lifecycle_id,
                        "lifecycle_epoch": lifecycle.epoch,
                        "token_digest": sha256(token.encode("utf-8")).hexdigest(),
                        "join_id_at_issue": join.id if join else None,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            payload = payload_json.encode("utf-8")
            repository.record_issuance(session, payload_json=payload_json, actor_id=actor_id)
            session.commit()
            stored = True
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except Exception:
            # No SQL retry/rollback/reissue. Even commit-then-error is closed;
            # caller recovery must prove this exact issuance before any future action.
            pass
        if not stored:
            token = payload = payload_json = None
            if signal == "interrupt":
                raise KeyboardInterrupt("invitation issuance interrupted") from None
            if signal == "exit":
                raise SystemExit(1) from None
            raise InvitationIssuanceError("invitation_issuance_unconfirmed") from None
        outcome = InvitationPublisher(redis).publish(token=token, payload=payload, ttl=hours * 3600)
        if outcome is not PublicationOutcome.PUBLISHED:
            token = payload = payload_json = None
            raise InvitationIssuanceError(f"invitation_publication_{outcome.value}") from None
        return token
