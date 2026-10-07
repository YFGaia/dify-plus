"""Installed SDK wrappers with memory sinks and local fake Redis execution."""

import json
import socket
from contextlib import contextmanager
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sentry_sdk
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.redis import _traced_execute_factory
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.redis._sync_common import patch_redis_client
from sentry_sdk.integrations.redis.modules.queries import _set_db_data
from sentry_sdk.integrations.redis.redis_cluster import _set_cluster_db_data
from sentry_sdk.scope import use_isolation_scope, use_scope
from sentry_sdk.transport import Transport
from wrapt import FunctionWrapper

from core.casdoor.auth_transactions import AuthTransactionError, AuthTransactionStore
from libs import sensitive_redis as owner

SECRET = "synthetic-private-redis-value-I28"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    denied = Mock(side_effect=AssertionError("offline test attempted network access"))
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield
    denied.assert_not_called()


class Envelopes(Transport):
    def __init__(self):
        super().__init__()
        self.payloads = []

    def capture_envelope(self, envelope):
        self.payloads.append(envelope.serialize())


@pytest.fixture
def telemetry(monkeypatch):
    sink = Envelopes()
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        transport=sink,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[],
        traces_sample_rate=1.0,
        enable_backpressure_handling=False,
        auto_session_tracking=False,
    )
    client.integrations = {"redis": RedisIntegration(max_data_size=None)}
    global_scope = sentry_sdk.get_global_scope()
    monkeypatch.setattr(global_scope, "client", client)
    global_attributes = dict(global_scope._attributes)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield client, sink, exporter, provider
        assert global_scope.client is client
        assert global_scope._attributes == global_attributes
    finally:
        client.close()
        provider.shutdown()


@pytest.mark.parametrize("cluster", [False, True], ids=["standalone", "cluster"])
@pytest.mark.parametrize("failure", [None, ValueError, BaseException, KeyboardInterrupt, SystemExit])
def test_real_redis_wrappers_suppress_and_restore(telemetry, caplog, cluster, failure):
    client, sink, exporter, provider = telemetry
    request_hook = Mock()
    response_hook = Mock()
    executed = []

    class LocalRedis:
        connection_pool = SimpleNamespace(connection_kwargs={"host": "localhost", "port": 6379, "db": 0})

        def get_default_node(self):
            return SimpleNamespace(host="localhost", port=6379)

        def execute_command(self, *args):
            executed.append(args)
            if SECRET in args and failure is not None:
                raise failure(SECRET)
            return args[-1]

        def eval(self, script, numkeys, *args):
            assert not is_instrumentation_enabled()
            assert sentry_sdk.get_client() is not client
            assert sentry_sdk.get_current_scope() is not current
            assert sentry_sdk.get_isolation_scope() is not isolation
            assert sentry_sdk.get_current_scope().span is None
            assert sentry_sdk.get_isolation_scope().span is None
            return self.execute_command("EVAL", script, numkeys, *args)

    # These are the installed entrypoint wrappers, patched only on this local
    # class; the cluster switch selects its real Sentry cluster metadata owner.
    patch_redis_client(LocalRedis, cluster, _set_cluster_db_data if cluster else _set_db_data)
    LocalRedis.execute_command = FunctionWrapper(
        LocalRedis.execute_command,
        _traced_execute_factory(provider.get_tracer(__name__), request_hook, response_hook),
    )
    redis = LocalRedis()
    store = object.__new__(AuthTransactionStore)
    store._redis = redis
    isolation = sentry_sdk.Scope()
    current = sentry_sdk.Scope()
    isolation.set_tag("caller", "preserved")
    current.set_extra("caller", "preserved")
    parent_otel = otel_context.get_current()
    with use_isolation_scope(isolation), use_scope(current):
        with sentry_sdk.start_transaction(name="ordinary-parent", sampled=True):
            parent_span = current.span
            redis.execute_command("EVAL", "ordinary-before", 1, "public-key", "public-before")
            before_spans = len(exporter.get_finished_spans())
            before_hooks = request_hook.call_count, response_hook.call_count
            error = None
            try:
                assert store._eval("return ARGV[1]", ("private-key",), SECRET) == SECRET
            except BaseException as caught:
                error = caught
            if failure is None:
                assert error is None
            else:
                expected = failure if failure in (KeyboardInterrupt, SystemExit) else AuthTransactionError
                assert type(error) is expected
                assert SECRET not in str(error)
                assert error.__cause__ is None
                assert error.__context__ is None
            assert len(exporter.get_finished_spans()) == before_spans
            assert (request_hook.call_count, response_hook.call_count) == before_hooks
            assert sentry_sdk.get_client() is client
            assert sentry_sdk.get_current_scope() is current
            assert sentry_sdk.get_isolation_scope() is isolation
            assert current.span is parent_span
            assert isolation._tags == {"caller": "preserved"}
            assert current._extras == {"caller": "preserved"}
            assert otel_context.get_current() is parent_otel
            redis.execute_command("EVAL", "ordinary-after", 1, "public-key", "public-after")
    assert len(executed) == 3
    assert request_hook.call_count == response_hook.call_count == 2
    assert len(exporter.get_finished_spans()) == 2
    spans = [dict(span.attributes) for span in exporter.get_finished_spans()]
    assert all(span["db.statement"].startswith("EVAL") for span in spans)
    assert "ordinary-before" in str(request_hook.call_args_list)
    assert "ordinary-after" in str(request_hook.call_args_list)
    assert SECRET not in str(request_hook.call_args_list)
    assert SECRET not in str(response_hook.call_args_list)
    assert SECRET not in str(spans)
    assert len(sink.payloads) == 1
    transaction = json.loads(sink.payloads[0].splitlines()[2])
    assert len(transaction["spans"]) == 2
    assert all(span["op"] == "db.redis" for span in transaction["spans"])
    assert SECRET.encode() not in b"".join(sink.payloads)
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "package", ["sentry-sdk", "opentelemetry-instrumentation", "opentelemetry-instrumentation-redis"]
)
def test_unsupported_versions_never_enter_body(monkeypatch, package):
    monkeypatch.setattr(owner, "version", lambda name: "unsupported" if name == package else version(name))
    body = Mock()
    with pytest.raises(owner.SensitiveRedisError, match="^sensitive_redis_uncertain$") as caught:
        with owner.sensitive_redis_call():
            body()
    body.assert_not_called()
    assert caught.value.__context__ is None


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_context_failure_restores_other_contexts(monkeypatch, phase):
    import sentry_sdk.scope

    original = sentry_sdk.scope.use_scope
    prior_current = sentry_sdk.get_current_scope()
    prior_isolation = sentry_sdk.get_isolation_scope()
    prior_otel = otel_context.get_current()
    body = Mock()

    @contextmanager
    def broken_scope(scope):
        if phase == "setup":
            raise RuntimeError(SECRET)
        with original(scope):
            yield
        raise RuntimeError(SECRET)

    monkeypatch.setattr(sentry_sdk.scope, "use_scope", broken_scope)
    with pytest.raises(owner.SensitiveRedisError, match="^sensitive_redis_uncertain$") as caught:
        with owner.sensitive_redis_call():
            body()
    assert body.call_count == (1 if phase == "teardown" else 0)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentry_sdk.get_current_scope() is prior_current
    assert sentry_sdk.get_isolation_scope() is prior_isolation
    assert otel_context.get_current() is prior_otel


def test_installed_versions_match_reviewed_lock():
    assert version("sentry-sdk") == "2.57.0"
    assert version("opentelemetry-instrumentation") == "0.65b0"
    assert version("opentelemetry-instrumentation-redis") == "0.65b0"


def test_nested_context_restores_outer_suppression():
    with owner.sensitive_redis_call():
        outer = sentry_sdk.get_current_scope()
        with owner.sensitive_redis_call():
            assert not is_instrumentation_enabled()
        assert sentry_sdk.get_current_scope() is outer
        assert not is_instrumentation_enabled()
