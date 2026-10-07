"""Private, one-request Redis ownership with finite configured waits.

This proves no physical 45-second bound: DNS multi-address resolution, TLS,
AUTH/HELLO/SELECT, parser/OS behavior, remote effects and local socket close need
runtime evidence. Late results are rejected, never retried by this owner.
"""

import math
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from queue import Full

from extensions.ext_redis import RedisClientWrapper
from libs.sensitive_redis import sensitive_redis_call
from redis import BlockingConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.connection import Connection, SSLConnection
from redis.driver_info import DriverInfo
from redis.exceptions import NoScriptError
from redis.maint_notifications import MaintNotificationsConfig
from redis.retry import Retry


class CasdoorRedisUnavailable(Exception):
    """Fixed error; never expose a Redis key, value, credential or cause."""

    def __init__(self):
        super().__init__("casdoor_redis_unavailable")


def _private_call(operation):
    # Redis Lock's installed Script owner may load a missing script. NOSCRIPT
    # confirms that EVALSHA did not execute; retain only this fixed typed signal.
    missing_script = False
    failed = False
    try:
        with sensitive_redis_call():
            try:
                result = operation()
            except NoScriptError:
                missing_script = True
    except Exception:
        failed = True
    if failed:
        raise CasdoorRedisUnavailable() from None
    if missing_script:
        raise NoScriptError("casdoor_script_missing") from None
    return result


class _Budget:
    def __init__(self, deadline, clock, command_cap, cleanup_reserve):
        self.deadline = deadline
        self.clock = clock
        self.command_cap = command_cap
        self.cleanup_reserve = cleanup_reserve
        self.cleaning = False
        self.closed = False
        self.business_failed = False
        self.cleanup_failed = False

    def remaining(self):
        remaining = self.deadline - self.clock() - (0 if self.cleaning else self.cleanup_reserve)
        if (
            self.closed
            or (self.business_failed and not self.cleaning)
            or (self.cleanup_failed and self.cleaning)
            or not math.isfinite(remaining)
            or remaining <= 0
        ):
            raise CasdoorRedisUnavailable()
        return min(self.command_cap, remaining)


class _ConnectionBudget:
    """Recompute before initialization, socket send/read and pool probes."""

    def __init__(self, *, request_budget, **kwargs):
        self._request_budget = request_budget
        self._owned_sockets = []
        super().__init__(**kwargs)

    def _refresh_budget(self):
        timeout = self._request_budget.remaining()
        self.socket_connect_timeout = self.socket_timeout = timeout
        if self._sock is not None:
            self._sock.settimeout(timeout)
        return timeout

    def _connect(self):
        self._refresh_budget()
        sock = super()._connect()
        self._owned_sockets.append(sock)
        return sock

    def connect(self):
        self._refresh_budget()
        result = _private_call(lambda: super(_ConnectionBudget, self).connect())
        self._refresh_budget()
        return result

    def send_packed_command(self, command, check_health=True):
        self._refresh_budget()
        result = _private_call(lambda: super(_ConnectionBudget, self).send_packed_command(command, check_health=False))
        self._refresh_budget()
        return result

    def read_response(self, **kwargs):
        self._refresh_budget()
        result = _private_call(lambda: super(_ConnectionBudget, self).read_response(**kwargs))
        self._refresh_budget()
        return result

    def can_read(self, timeout=0):
        remaining = self._refresh_budget()
        result = _private_call(lambda: super(_ConnectionBudget, self).can_read(timeout=min(timeout, remaining)))
        self._refresh_budget()
        return result


class _RequestConnection(_ConnectionBudget, Connection):
    pass


class _RequestTLSConnection(_ConnectionBudget, SSLConnection):
    pass


class _RequestPool(BlockingConnectionPool):
    def __init__(self, *, request_budget, **kwargs):
        self._request_budget = request_budget
        super().__init__(request_budget=request_budget, **kwargs)

    def get_connection(self, command_name=None, *keys, **options):
        # The generic pool reconnects an unready socket. This one-use pool must
        # fail instead; it never makes a second initialization attempt.
        timeout = self._request_budget.remaining()
        self.timeout = timeout
        connection = None
        acquired = False
        try:
            connection = self.pool.get(block=True, timeout=timeout)
            acquired = True
            self._request_budget.remaining()
            if connection is None:
                connection = self.make_connection()
            connection.connect()
            if connection.can_read():
                raise CasdoorRedisUnavailable()
            self._request_budget.remaining()
            return connection
        except BaseException:
            if connection is not None:
                self.release(connection)
            elif acquired:
                # Restore an unused slot even when construction failed.
                try:
                    self.pool.put_nowait(None)
                except Full:
                    pass
            raise


class _RequestRedis(Redis):
    def __init__(self, *, request_budget, **kwargs):
        self._request_budget = request_budget
        super().__init__(**kwargs)

    def execute_command(self, *args, **options):
        self._request_budget.remaining()
        try:
            result = _private_call(lambda: super(_RequestRedis, self).execute_command(*args, **options))
            self._request_budget.remaining()
            return result
        except NoScriptError:
            raise
        except BaseException:
            if self._request_budget.cleaning:
                self._request_budget.cleanup_failed = True
            else:
                self._request_budget.business_failed = True
            raise


class _RequestClient(RedisClientWrapper):
    def __init__(self, client, prefix, budget):
        super().__init__()
        self.initialize(client)
        self._prefix = prefix
        self._budget = budget

    def _get_prefix(self):
        return self._prefix

    @contextmanager
    def _casdoor_cleanup_scope(self) -> Iterator[None]:
        previous = self._budget.cleaning
        self._budget.cleaning = True
        try:
            self._budget.remaining()
            yield
        finally:
            self._budget.cleaning = previous


class CasdoorRedisRequestScope:
    """One private pool, one client, one idempotent terminal close outcome."""

    def __init__(self, *, deadline, clock, command_cap, cleanup_reserve, parameters, prefix, connection_class):
        self._budget = _Budget(deadline, clock, command_cap, cleanup_reserve)
        self._pool = None
        self._raw_client = None
        self._finished = False
        self._close_ok = False
        try:
            timeout = self._budget.remaining()

            def build_pool():
                self._pool = _RequestPool(
                    request_budget=self._budget,
                    connection_class=connection_class,
                    max_connections=1,
                    timeout=timeout,
                    socket_timeout=timeout,
                    socket_connect_timeout=timeout,
                    retry=Retry(NoBackoff(), 0),
                    retry_on_timeout=False,
                    retry_on_error=[],
                    health_check_interval=0,
                    maint_notifications_config=MaintNotificationsConfig(enabled=False),
                    **parameters,
                )

            def build_client():
                self._raw_client = _RequestRedis(request_budget=self._budget, connection_pool=self._pool)

            _private_call(build_pool)
            _private_call(build_client)
            self.client = _RequestClient(self._raw_client, prefix, self._budget)
        except BaseException:
            self.finish()
            raise

    def finish(self) -> bool:
        if self._finished:
            return self._close_ok
        self._finished = self._budget.closed = True
        healthy = True
        if self._raw_client is not None:
            try:
                _private_call(self._raw_client.close)
            except BaseException:
                healthy = False
        if self._pool is not None:
            try:
                _private_call(self._pool.disconnect)
            except BaseException:
                healthy = False
            # redis-py clears _sock before close, so inspect retained socket
            # objects too. A close exception/unknown descriptor denies delivery.
            for connection in self._pool._connections:
                try:
                    if connection._sock is not None or any(sock.fileno() != -1 for sock in connection._owned_sockets):
                        healthy = False
                except BaseException:
                    healthy = False
        try:
            self._close_ok = healthy and self._budget.clock() < self._budget.deadline
        except BaseException:
            self._close_ok = False
        return self._close_ok


class CasdoorRedisRuntimeFactory:
    """Store configuration lazily; never inspect or reuse the shared client."""

    def __init__(
        self,
        settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        command_cap: float = 5.0,
        cleanup_reserve: float = 1.0,
    ):
        self._settings = settings
        self._clock = clock
        self._command_cap = command_cap
        self._cleanup_reserve = cleanup_reserve

    def open(self, *, deadline: float) -> CasdoorRedisRequestScope:
        try:
            settings = self._settings
            if (
                settings.REDIS_USE_SENTINEL
                or settings.REDIS_USE_CLUSTERS
                or settings.REDIS_ENABLE_CLIENT_SIDE_CACHE
                or any(
                    getattr(settings, name, None)
                    for name in ("REDIS_URL", "REDIS_UNIX_SOCKET_PATH", "REDIS_CONNECTION_CLASS")
                )
                or type(settings.REDIS_HOST) is not str
                or not settings.REDIS_HOST
                or "/" in settings.REDIS_HOST
                or settings.REDIS_SERIALIZATION_PROTOCOL not in (2, 3)
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                    for v in (deadline, self._command_cap, self._cleanup_reserve)
                )
            ):
                raise CasdoorRedisUnavailable()
            parameters = dict(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                username=settings.REDIS_USERNAME,
                password=settings.REDIS_PASSWORD,
                db=settings.REDIS_DB,
                protocol=settings.REDIS_SERIALIZATION_PROTOCOL,
                encoding="utf-8",
                encoding_errors="strict",
                decode_responses=False,
                driver_info=DriverInfo(name="", lib_version=""),
            )
            connection_class = _RequestConnection
            if settings.REDIS_USE_SSL:
                if settings.REDIS_SSL_CERT_REQS != "CERT_REQUIRED":
                    raise CasdoorRedisUnavailable()
                connection_class = _RequestTLSConnection
                parameters.update(
                    ssl_cert_reqs=ssl.CERT_REQUIRED,
                    ssl_check_hostname=True,
                    ssl_ca_certs=settings.REDIS_SSL_CA_CERTS,
                    ssl_certfile=settings.REDIS_SSL_CERTFILE,
                    ssl_keyfile=settings.REDIS_SSL_KEYFILE,
                )
            return CasdoorRedisRequestScope(
                deadline=deadline,
                clock=self._clock,
                command_cap=self._command_cap,
                cleanup_reserve=self._cleanup_reserve,
                parameters=parameters,
                prefix=settings.REDIS_KEY_PREFIX,
                connection_class=connection_class,
            )
        except Exception:
            pass
        raise CasdoorRedisUnavailable() from None
