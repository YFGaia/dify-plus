"""Synthetic actual-owner privacy and bounded-stream checks; no physical sockets."""

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from unittest.mock import Mock

import anyio
import httpx
import pytest
import sentry_sdk
from httpcore._trace import Trace
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sentry_sdk.client import BaseClient
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scope import use_isolation_scope, use_scope
from sentry_sdk.transport import Transport

from core.file import remote_fetcher as owner
from core.helper import ssrf_proxy

SENTINEL = "I23_B0_QUERY_SECRET_8dfa"
PRIVATE_URL = "https://avatar.example.invalid/image?token=" + SENTINEL
PUBLIC_URL = "https://ordinary.example.invalid/provider?public=visible"


@pytest.fixture(autouse=True)
def configured_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner.dify_config, "SSRF_PROXY_ALL_URL", "http://proxy.example.invalid:3128")
    monkeypatch.setattr(owner.dify_config, "FILES_URL", "https://files.example.invalid")
    monkeypatch.setattr(owner.dify_config, "INTERNAL_FILES_URL", "https://internal-files.example.invalid")


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], mode: str = "ok") -> None:
        self.chunks = chunks
        self.mode = mode
        self.yielded = 0
        self.closed = False
        self.cancelled = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            for chunk in self.chunks:
                self.yielded += 1
                yield chunk
                if self.mode == "drip":
                    await anyio.sleep(0.025)
            if self.mode == "wait":
                await anyio.sleep_forever()
            if self.mode == "cancel":
                raise asyncio.CancelledError(PRIVATE_URL)
            if self.mode == "task_cancel":
                asyncio.current_task().cancel(PRIVATE_URL)
                await anyio.sleep(0)
            if self.mode == "interrupt":
                raise KeyboardInterrupt(PRIVATE_URL)
            if self.mode == "exit":
                raise SystemExit(PRIVATE_URL)
            if self.mode == "error":
                error = httpx.ReadError(PRIVATE_URL)
                error.secret_attribute = PRIVATE_URL
                raise error from ValueError(PRIVATE_URL)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        if self.mode == "close_error":
            raise RuntimeError(PRIVATE_URL)
        if self.mode == "close_slow":
            await anyio.sleep(0.2)
        self.closed = True


class MemoryTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        body: Body,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        entered: threading.Event | None = None,
        released: threading.Event | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []
        self.closed = False
        self.entered = entered
        self.released = released

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.entered is not None:
            self.entered.set()
            assert self.released is not None and self.released.wait(3)
        # Installed HTTPCore Trace constructs exception/return repr BEFORE logger
        # filtering. This is intentionally not a made-up privacy wrapper.
        for name in owner._SENSITIVE_HTTP_LOGGERS[1:]:
            async with Trace("receive_response_headers", logging.getLogger(name)) as trace:
                trace.return_value = (str(request.url), self.headers)
            try:
                async with Trace("receive_response_body", logging.getLogger(name)):
                    raise ValueError(str(request.url))
            except ValueError:
                pass
        return httpx.Response(self.status, headers=self.headers, stream=self.body, request=request)

    async def aclose(self) -> None:
        self.closed = True


def install_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", lambda: client)
    return client


def fetch() -> owner.BoundedExternalFile:
    return owner.fetch_bounded_external_file(lambda: PRIVATE_URL, time.monotonic() + 2)


@pytest.mark.parametrize("value", [None, False, "1", float("nan"), float("inf"), float("-inf")])
def test_invalid_deadline_before_supplier(value: object) -> None:
    supplier = Mock()
    assert owner.fetch_bounded_external_file(supplier, value).reason == "invalid_arguments"
    supplier.assert_not_called()


def test_expiry_noncallable_and_async_runtime_before_supplier() -> None:
    supplier = Mock()
    assert owner.fetch_bounded_external_file(supplier, 10**1000).reason == "invalid_arguments"
    assert owner.fetch_bounded_external_file(supplier, time.monotonic() - 1).reason == "expired"
    assert owner.fetch_bounded_external_file(object(), time.monotonic() + 1).reason == "invalid_arguments"

    async def scenario() -> None:
        assert owner.fetch_bounded_external_file(supplier, time.monotonic() + 1).reason == "runtime_unsupported"

    anyio.run(scenario)
    supplier.assert_not_called()


@pytest.mark.parametrize("https_proxy", [None, "http://proxy.example.invalid:3128"])
def test_proxy_required_before_supplier(monkeypatch: pytest.MonkeyPatch, https_proxy: str | None) -> None:
    monkeypatch.setattr(owner.dify_config, "SSRF_PROXY_ALL_URL", None)
    monkeypatch.setattr(owner.dify_config, "SSRF_PROXY_HTTP_URL", None)
    monkeypatch.setattr(owner.dify_config, "SSRF_PROXY_HTTPS_URL", https_proxy)
    supplier = Mock()
    assert owner.fetch_bounded_external_file(supplier, time.monotonic() + 1).reason == "proxy_required"
    supplier.assert_not_called()


@pytest.mark.parametrize("dependency", ["sentry-sdk", "opentelemetry-instrumentation", "transport", "scope"])
def test_unsupported_runtime_before_supplier(monkeypatch: pytest.MonkeyPatch, dependency: str) -> None:
    if dependency == "transport":
        monkeypatch.setattr(ssrf_proxy, "_DEADLINE_RUNTIME_VERSIONS", ("changed", "1.0.9", "4.14.1"))
    elif dependency == "scope":
        monkeypatch.setattr("sentry_sdk.scope.use_scope", None)
    else:
        original = owner.version
        monkeypatch.setattr(owner, "version", lambda name: "unsupported" if name == dependency else original(name))
    supplier = Mock()
    outcome = owner.fetch_bounded_external_file(supplier, time.monotonic() + 1)
    assert outcome.reason in {"privacy_unsupported", "runtime_unsupported"}
    supplier.assert_not_called()
    assert owner._sensitive_file_request.get() is False


@pytest.mark.parametrize(
    "value",
    [
        None,
        7,
        "",
        "http://avatar.example.invalid/a",
        "https://user:pass@avatar.example.invalid/a",
        "https://avatar.example.invalid:444/a",
        "https://avatar.example.invalid:bad/a",
        "https://avatar.example.invalid/a#",
        "https://avatar.example.invalid/a%0a",
        "https://avatar.example.invalid/a%5c",
        "https://avatar.example.invalid/a%gg",
        "https://avatar.example.invalid/a%ff",
        "https://avatar.example.invalid/\n",
        "https://avatar.example.invalid/\\x",
        "https://avatar.example.invalid/\ud800",
        "https://avatar.example.invalid/" + "x" * 2049,
        "https://avatar.example.invalid/?q=" + "x" * 4096,
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://10.0.0.1/a",
        "https://224.0.0.1/a",
        "https://[::1]/a",
        "https://[ff02::1]/a",
        "https://[fe80::1%25en0]/a",
        "https://localhost/a",
        "https://foo.local/a",
        "https://foo.internal/a",
        "https://foo.localhost/a",
        "https://2130706433/a",
        "https://0177.0.0.1/a",
        "https://foo.123/a",
        "https://foo..invalid/a",
        "https://%61vatar.example.invalid/a",
        "https://头像.example.invalid/a",
    ],
)
def test_bad_url_never_builds_client(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    build = Mock()
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", build)
    result = owner.fetch_bounded_external_file(lambda: value, time.monotonic() + 1)
    assert result.reason == "invalid_url" and result.termination == "confirmed"
    build.assert_not_called()


@pytest.mark.parametrize("origin", ["files.example.invalid", "internal-files.example.invalid"])
def test_local_signed_origin_never_resolves_or_builds(monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    forbidden = Mock(side_effect=AssertionError("no local or network work"))
    monkeypatch.setattr(owner, "_resolve_dify_signed_file_url", forbidden)
    monkeypatch.setattr(owner.storage, "load_once", forbidden)
    monkeypatch.setattr(ssrf_proxy, "_build_deadline_ssrf_client", forbidden)
    outcome = owner.fetch_bounded_external_file(
        lambda: f"https://{origin}/files/11111111-1111-1111-1111-111111111111/file-preview?sign={SENTINEL}",
        time.monotonic() + 1,
    )
    assert outcome.reason == "local_origin"
    forbidden.assert_not_called()


@pytest.mark.parametrize("chunks", [[b"one", b"two"], [b"x" * (2 * 1024 * 1024)], []])
def test_actual_original_owner_success_policy_and_closed_bytes(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]
) -> None:
    body = Body(chunks)
    transport = MemoryTransport(body, headers={"x-secret": SENTINEL})
    client = install_client(monkeypatch, transport)
    result = fetch()
    assert result.status == "ok" and result.content == b"".join(chunks)
    assert body.closed and transport.closed and client.is_closed
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "GET" and request.headers["accept-encoding"] == "identity"
    assert request.headers["host"] == "avatar.example.invalid"
    assert not any(k in request.headers for k in ("authorization", "cookie", "sentry-trace", "baggage"))
    assert max(request.extensions["timeout"].values()) <= 15
    assert {f.name for f in fields(result)} == {"status", "reason", "termination", "content"}
    assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    ("mode", "status", "headers", "chunks", "reason", "termination"),
    [
        ("ok", 302, {"location": PRIVATE_URL}, [b"redirect"], "http_status", "confirmed"),
        ("ok", 503, {}, [b"error"], "http_status", "confirmed"),
        ("ok", 403, {"server": "Squid"}, [b"denied"], "proxy_denied", "confirmed"),
        ("ok", 200, {"content-encoding": "gzip"}, [b"encoded"], "encoded_response", "confirmed"),
        ("ok", 200, {}, [b"x" * (2 * 1024 * 1024), b"y", b"unread"], "too_large", "confirmed"),
        ("error", 200, {}, [b"partial"], "request_failed", "confirmed"),
        ("cancel", 200, {}, [b"partial"], "cancelled", "confirmed"),
        ("task_cancel", 200, {}, [b"partial"], "cancelled", "confirmed"),
        ("close_error", 200, {}, [b"ok"], "termination_unconfirmed", "unconfirmed"),
        ("close_slow", 200, {}, [b"ok"], "termination_unconfirmed", "unconfirmed"),
        ("wait", 200, {}, [], "expired", "confirmed"),
        ("drip", 200, {}, [b"x"] * 100, "expired", "confirmed"),
    ],
)
def test_bounded_stream_rejections_and_real_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: int,
    headers: dict[str, str],
    chunks: list[bytes],
    reason: str,
    termination: str,
) -> None:
    body = Body(chunks, mode)
    transport = MemoryTransport(body, status=status, headers=headers)
    client = install_client(monkeypatch, transport)
    started = time.monotonic()
    outcome = owner.fetch_bounded_external_file(lambda: PRIVATE_URL, started + 0.16)
    assert outcome.reason == reason and outcome.termination == termination and outcome.content == b""
    assert time.monotonic() - started < 0.5
    assert transport.closed and client.is_closed and len(transport.requests) == 1
    if termination == "confirmed":
        assert body.closed
    if reason == "too_large":
        assert body.yielded == 2  # Does not consume the remainder after cap.
    if mode in {"drip", "wait", "cancel", "task_cancel"}:
        assert body.cancelled
    assert SENTINEL not in repr(outcome)


class Envelopes(Transport):
    def __init__(self) -> None:
        super().__init__({"dsn": "https://public@example.invalid/1"})
        self.payloads: list[bytes] = []

    def capture_envelope(self, envelope) -> None:
        self.payloads.append(envelope.serialize())


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch):
    """Installed wrappers/real recording client with memory-only sinks, restored."""
    # Record originals before the installed setup_once functions patch them.
    monkeypatch.setattr(httpx.AsyncClient, "send", httpx.AsyncClient.send)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(logging.Logger, "callHandlers", logging.Logger.callHandlers)
    HttpxIntegration.setup_once()
    LoggingIntegration.setup_once()
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
        include_local_variables=True,
    )
    client.integrations = {"httpx": HttpxIntegration(), "logging": LoggingIntegration(level=logging.DEBUG)}
    # Exercise the actual get_client global fallback, not only recording parent
    # scopes. Test-only mutations are restored; source cannot mutate this scope.
    global_scope = sentry_sdk.get_global_scope()
    monkeypatch.setattr(global_scope, "client", client)
    monkeypatch.setattr(global_scope, "_attributes", dict(global_scope._attributes))
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield client, sink, exporter, provider
    finally:
        client.close()
        provider.shutdown()


@pytest.mark.parametrize("mode", ["ok", "error", "cancel", "close_error", "supplier_error", "supplier_cancel"])
def test_installed_telemetry_parent_recorder_and_concurrent_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, telemetry, mode: str
) -> None:
    client, sink, exporter, provider = telemetry
    caplog.set_level(logging.DEBUG)
    original_current = sentry_sdk.get_current_scope()
    original_isolation = sentry_sdk.get_isolation_scope()
    original_otel = otel_context.get_current()
    parent_current = sentry_sdk.Scope(client=client)
    parent_isolation = sentry_sdk.Scope(client=client)
    processor_events: list[dict] = []

    def processor(event, hint):
        processor_events.append(event)
        return event

    parent_current.add_event_processor(processor)
    parent_current.set_extra("parent_extra", "retained")
    parent_isolation.set_extra("parent_isolation", "retained")
    entered = threading.Event()
    released = threading.Event()
    body = Body([SENTINEL.encode()], mode if not mode.startswith("supplier") else "ok")
    private_transport = MemoryTransport(body, headers={"x-url": PRIVATE_URL}, entered=entered, released=released)
    install_client(monkeypatch, AsyncOpenTelemetryTransport(private_transport, tracer_provider=provider))
    ordinary_transport = MemoryTransport(Body([b"ordinary"]))
    ordinary_client = httpx.AsyncClient(
        transport=AsyncOpenTelemetryTransport(ordinary_transport, tracer_provider=provider)
    )

    with use_isolation_scope(parent_isolation), use_scope(parent_current):
        with sentry_sdk.start_transaction(name="parent-recording", sampled=True) as transaction:
            parent_isolation.span = transaction  # Also exercise inherited isolation span.
            sentry_sdk.add_breadcrumb(message="parent-before")
            recorder = transaction._span_recorder
            assert recorder is not None
            global_scope = sentry_sdk.get_global_scope()
            global_scope.set_attribute("sentry.sdk.name", "ordinary-custom-value")
            global_attributes = dict(global_scope._attributes)
            context = contextvars.copy_context()

            def provider_request() -> None:
                assert entered.wait(3)
                try:

                    async def run() -> None:
                        await ordinary_client.get(PUBLIC_URL)
                        await ordinary_client.aclose()

                    anyio.run(run)
                    logging.getLogger("httpx").warning("ordinary provider survives")
                    sentry_sdk.capture_message("ordinary event survives")
                finally:
                    released.set()

            def supplier() -> str:
                assert sentry_sdk.get_current_scope() is not parent_current
                assert sentry_sdk.get_isolation_scope() is not parent_isolation
                assert not is_http_instrumentation_enabled()
                assert sentry_sdk.get_current_scope().span is None
                assert sentry_sdk.get_isolation_scope().span is None
                private_client = sentry_sdk.get_client()
                assert private_client is not client and private_client.is_active()
                assert global_scope.client is client and client.is_active()
                assert type(private_client).capture_event is BaseClient.capture_event
                assert type(private_client).get_integration is BaseClient.get_integration
                assert private_client.get_integration(HttpxIntegration) is None
                with sentry_sdk.start_span(name=PRIVATE_URL):
                    pass
                sentry_sdk.add_breadcrumb(message=PRIVATE_URL)
                # Capture through the actual SDK. No parent event processor or
                # recorder may see request, chained exception or local variables.
                try:
                    raise ValueError(PRIVATE_URL) from RuntimeError(PRIVATE_URL)
                except ValueError as error:
                    error.request = httpx.Request("GET", PRIVATE_URL)
                    sentry_sdk.capture_exception(error)
                if mode.startswith("supplier"):
                    entered.set()
                    assert released.wait(3)
                    if mode == "supplier_cancel":
                        raise asyncio.CancelledError(PRIVATE_URL)
                    raise RuntimeError(PRIVATE_URL)
                return PRIVATE_URL

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(context.run, provider_request)
                outcome = owner.fetch_bounded_external_file(supplier, time.monotonic() + 4)
                future.result(timeout=4)
            assert sentry_sdk.get_current_scope() is parent_current
            assert sentry_sdk.get_isolation_scope() is parent_isolation
            assert sentry_sdk.get_current_scope().span is transaction
            assert sentry_sdk.get_global_scope() is global_scope
            assert global_scope.client is client and global_scope._attributes == global_attributes
            assert is_http_instrumentation_enabled()
            assert not owner._sensitive_file_request.get()
            assert any("ordinary.example.invalid" in str(span.to_json()) for span in recorder.spans)
            assert SENTINEL not in repr([span.to_json() for span in recorder.spans])
            assert parent_current._extras == {"parent_extra": "retained"}
            sentry_sdk.capture_message("after-private")
        assert SENTINEL not in repr(parent_isolation._breadcrumbs)
    assert sentry_sdk.get_current_scope() is original_current
    assert sentry_sdk.get_isolation_scope() is original_isolation
    assert otel_context.get_current() is original_otel
    assert SENTINEL not in repr(outcome)
    assert (
        outcome.reason
        == {
            "ok": "ok",
            "error": "request_failed",
            "cancel": "cancelled",
            "close_error": "termination_unconfirmed",
            "supplier_error": "supplier_failed",
            "supplier_cancel": "cancelled",
        }[mode]
    )
    assert SENTINEL not in caplog.text
    assert "ordinary provider survives" in caplog.text
    assert "ordinary.example.invalid" in caplog.text
    spans = exporter.get_finished_spans()
    assert spans and all(SENTINEL not in repr(span.attributes) + repr(span.events) for span in spans)
    assert any("ordinary.example.invalid" in repr(span.attributes) for span in spans)
    assert sink.payloads and all(SENTINEL.encode() not in payload for payload in sink.payloads)
    assert any(b"parent-recording" in payload for payload in sink.payloads)
    assert any(b"ordinary event survives" in payload for payload in sink.payloads)
    assert processor_events and SENTINEL not in repr(processor_events)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["supplier", "stream"])
def test_shutdown_reconstructed_after_private_context_without_raw_chain(
    monkeypatch: pytest.MonkeyPatch, telemetry, kind: type[BaseException], stage: str
) -> None:
    client, sink, exporter, provider = telemetry
    current = sentry_sdk.get_current_scope()
    isolation = sentry_sdk.get_isolation_scope()
    otel = otel_context.get_current()

    def supplier() -> str:
        sensitive_local = PRIVATE_URL
        sentry_sdk.add_breadcrumb(message=sensitive_local)
        sentry_sdk.get_current_scope().set_extra("secret", sensitive_local)
        if stage == "stream":
            return PRIVATE_URL
        raise kind(sensitive_local) from ValueError(sensitive_local)

    transport = MemoryTransport(Body([], "interrupt" if kind is KeyboardInterrupt else "exit"))
    install_client(monkeypatch, AsyncOpenTelemetryTransport(transport, tracer_provider=provider))
    with use_isolation_scope(sentry_sdk.Scope(client=client)), use_scope(sentry_sdk.Scope(client=client)):
        with pytest.raises(kind) as caught:
            owner.fetch_bounded_external_file(supplier, time.monotonic() + 1)
        sentry_sdk.capture_exception(caught.value)
    signal = caught.value
    assert signal.__context__ is None and signal.__cause__ is None
    assert SENTINEL not in repr(signal.args)
    frame = signal.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_filename == owner.__file__:
            assert SENTINEL not in repr(frame.tb_frame.f_locals)
        frame = frame.tb_next
    assert current is sentry_sdk.get_current_scope() and isolation is sentry_sdk.get_isolation_scope()
    assert otel is otel_context.get_current() and not owner._sensitive_file_request.get()
    assert sink.payloads and all(SENTINEL.encode() not in payload for payload in sink.payloads)
    assert not exporter.get_finished_spans()
    if stage == "stream":
        assert transport.body.closed and transport.closed


def test_permanent_filters_are_idempotent_and_do_not_modify_levels_or_handlers() -> None:
    before = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers))
        for name in owner._SENSITIVE_HTTP_LOGGERS
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: owner._install_sensitive_file_log_filter(), range(32)))
    for name in owner._SENSITIVE_HTTP_LOGGERS:
        logger = logging.getLogger(name)
        assert sum(isinstance(f, owner._SensitiveFileLogFilter) for f in logger.filters) == 1
        assert (logger.level, logger.handlers) == before[name]
