"""Independent checks of the installed Redis telemetry wrappers."""

import json
import socket
from contextlib import contextmanager
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sentry_sdk
from core.casdoor.auth_transactions import AuthTransactionStore
from libs.sensitive_redis import SensitiveRedisError, sensitive_redis_call
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

PRIVATE = "independent-private-eval-7cf2"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    denied = Mock(
        side_effect=AssertionError("network forbidden in isolated verification")
    )
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield
    denied.assert_not_called()


class EnvelopeSink(Transport):
    def __init__(self):
        super().__init__()
        self.wire = []

    def capture_envelope(self, envelope):
        self.wire.append(envelope.serialize())


@pytest.fixture
def observers(monkeypatch):
    sink = EnvelopeSink()
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        transport=sink,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[],
        traces_sample_rate=1,
        enable_backpressure_handling=False,
        auto_session_tracking=False,
    )
    client.integrations = {"redis": RedisIntegration(max_data_size=None)}
    global_scope = sentry_sdk.get_global_scope()
    monkeypatch.setattr(global_scope, "client", client)
    original_global_attrs = dict(global_scope._attributes)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield client, sink, exporter, provider
        assert global_scope.client is client
        assert global_scope._attributes == original_global_attrs
    finally:
        client.close()
        provider.shutdown()


@pytest.mark.parametrize("cluster", [False, True], ids=["single-node", "cluster"])
def test_suppression_hides_eval_from_installed_wrappers_and_restores(
    observers, cluster
):
    client, sink, exporter, provider = observers
    before_hook, after_hook = Mock(), Mock()
    calls = []

    class LocalRedis:
        connection_pool = SimpleNamespace(
            connection_kwargs={"host": "127.0.0.1", "port": 6379, "db": 0}
        )

        def get_default_node(self):
            return SimpleNamespace(host="127.0.0.1", port=6379)

        def execute_command(self, *cmd):
            calls.append(cmd)
            return cmd[-1]

        def eval(self, source, key_count, *items):
            assert is_instrumentation_enabled() is False
            assert sentry_sdk.get_client() is not client
            assert sentry_sdk.get_current_scope() is not request_scope
            assert sentry_sdk.get_isolation_scope() is not tenant_scope
            return self.execute_command("EVAL", source, key_count, *items)

    patch_redis_client(
        LocalRedis, cluster, _set_cluster_db_data if cluster else _set_db_data
    )
    LocalRedis.execute_command = FunctionWrapper(
        LocalRedis.execute_command,
        _traced_execute_factory(
            provider.get_tracer("independent.redis"), before_hook, after_hook
        ),
    )
    redis = LocalRedis()
    store = object.__new__(AuthTransactionStore)
    store._redis = redis
    request_scope, tenant_scope = sentry_sdk.Scope(), sentry_sdk.Scope()
    request_scope.set_extra("trace-owner", "request")
    tenant_scope.set_tag("tenant-owner", "tenant")
    parent_context = otel_context.set_value("independent-parent", "preserve")
    parent_token = otel_context.attach(parent_context)
    inherited_context = otel_context.get_current()

    with use_isolation_scope(tenant_scope), use_scope(request_scope):
        with sentry_sdk.start_transaction(name="independent-parent", sampled=True):
            active_span = request_scope.span
            redis.execute_command(
                "EVAL", "ordinary-prelude", 1, "open-key", "open-value"
            )
            spans_before = len(exporter.get_finished_spans())
            hooks_before = before_hook.call_count, after_hook.call_count
            assert (
                store._eval("return ARGV[1]", ("credential-key",), PRIVATE) == PRIVATE
            )
            assert len(exporter.get_finished_spans()) == spans_before
            assert (before_hook.call_count, after_hook.call_count) == hooks_before
            assert sentry_sdk.get_client() is client
            assert sentry_sdk.get_current_scope() is request_scope
            assert sentry_sdk.get_isolation_scope() is tenant_scope
            assert request_scope.span is active_span
            assert otel_context.get_current() is inherited_context
            redis.execute_command(
                "EVAL", "ordinary-epilogue", 1, "open-key", "open-value"
            )

    otel_context.detach(parent_token)

    assert len(calls) == 3
    assert before_hook.call_count == after_hook.call_count == 2
    finished = exporter.get_finished_spans()
    assert len(finished) == 2
    assert all("db.statement" in span.attributes for span in finished)
    assert PRIVATE not in str([span.attributes for span in finished])
    assert "ordinary-prelude" in str(before_hook.call_args_list)
    assert "ordinary-epilogue" in str(before_hook.call_args_list)
    assert PRIVATE not in str(before_hook.call_args_list)
    assert PRIVATE not in str(after_hook.call_args_list)
    assert len(sink.wire) == 1
    transaction = json.loads(sink.wire[0].splitlines()[2])
    assert len(transaction["spans"]) == 2
    assert PRIVATE.encode() not in b"".join(sink.wire)


def test_failed_setup_is_sanitized_before_body(monkeypatch):
    import sentry_sdk.scope

    before_current = sentry_sdk.get_current_scope()
    before_isolation = sentry_sdk.get_isolation_scope()
    before_otel = otel_context.get_current()

    @contextmanager
    def scope_fails_to_enter(_scope):
        raise RuntimeError(PRIVATE)
        yield

    monkeypatch.setattr(sentry_sdk.scope, "use_scope", scope_fails_to_enter)
    yielded = []
    with pytest.raises(SensitiveRedisError) as setup_error:
        with sensitive_redis_call():
            yielded.append(True)
    assert not yielded
    assert setup_error.value.__cause__ is setup_error.value.__context__ is None
    assert sentry_sdk.get_current_scope() is before_current
    assert sentry_sdk.get_isolation_scope() is before_isolation
    assert otel_context.get_current() is before_otel


def test_expected_sdk_build_is_present():
    assert version("sentry-sdk") == "2.57.0"
    assert version("opentelemetry-instrumentation") == "0.65b0"
    assert version("opentelemetry-instrumentation-redis") == "0.65b0"
