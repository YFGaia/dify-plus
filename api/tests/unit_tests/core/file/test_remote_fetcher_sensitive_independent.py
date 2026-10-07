"""Independent installed-SDK fallback checks for the sensitive file owner."""

import logging
import time

import anyio
import httpx
import pytest
import sentry_sdk
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from sentry_sdk.client import BaseClient
from sentry_sdk.scope import use_isolation_scope, use_scope

from core.file import remote_fetcher as owner
from tests.unit_tests.core.file import test_remote_fetcher_sensitive_extend as author_tests
from tests.unit_tests.core.file.test_remote_fetcher_sensitive_extend import (
    PRIVATE_URL,
    PUBLIC_URL,
    SENTINEL,
    Body,
    MemoryTransport,
    install_client,
)


@pytest.fixture
def telemetry(monkeypatch):
    yield from author_tests.telemetry.__wrapped__(monkeypatch)


def test_parent_scopes_without_clients_use_global_fallback_only_outside_private_fetch(
    monkeypatch, caplog, telemetry
) -> None:
    client, sink, exporter, provider = telemetry
    monkeypatch.setattr(owner.dify_config, "SSRF_PROXY_ALL_URL", "http://proxy.example.invalid:3128")
    caplog.set_level(logging.DEBUG)
    global_scope = sentry_sdk.get_global_scope()
    original_global_client = global_scope.client
    global_scope.set_attribute("sentry.sdk.name", "independent-global-fallback")
    global_attributes = dict(global_scope._attributes)
    parent_current = sentry_sdk.Scope()
    parent_isolation = sentry_sdk.Scope()
    processor_events = []
    parent_current.add_event_processor(lambda event, hint: processor_events.append(event) or event)

    body = Body([SENTINEL.encode()], "close_error")
    transport = MemoryTransport(body, headers={"x-private-url": PRIVATE_URL})
    private_client = install_client(monkeypatch, AsyncOpenTelemetryTransport(transport, tracer_provider=provider))
    observed_private_clients = []

    def supplier() -> str:
        assert sentry_sdk.get_client() is not client
        observed_private_clients.append(sentry_sdk.get_client())
        try:
            raise ValueError(PRIVATE_URL) from RuntimeError(PRIVATE_URL)
        except ValueError as error:
            error.request = httpx.Request("GET", PRIVATE_URL)
            sentry_sdk.capture_exception(error)
        sentry_sdk.add_breadcrumb(message=PRIVATE_URL)
        return PRIVATE_URL

    with use_isolation_scope(parent_isolation), use_scope(parent_current):
        assert parent_current.client is not client and parent_isolation.client is not client
        assert sentry_sdk.get_client() is client
        sentry_sdk.capture_message("global fallback before private fetch")

        outcome = owner.fetch_bounded_external_file(supplier, time.monotonic() + 2)

        assert outcome.reason == "termination_unconfirmed"
        assert outcome.termination == "unconfirmed"
        assert parent_current.client is not client and parent_isolation.client is not client
        assert sentry_sdk.get_client() is client
        sentry_sdk.capture_message("global fallback after private fetch")

    assert global_scope.client is original_global_client is client
    assert global_scope._attributes == global_attributes
    assert parent_current.client is not client and parent_isolation.client is not client
    assert not body.closed and transport.closed and private_client.is_closed
    assert len(observed_private_clients) == 1
    assert observed_private_clients[0] is not client
    assert type(observed_private_clients[0]).capture_event is BaseClient.capture_event
    assert SENTINEL not in caplog.text
    assert "global fallback before private fetch" in caplog.text or any(
        b"global fallback before private fetch" in payload for payload in sink.payloads
    )
    assert any(b"global fallback after private fetch" in payload for payload in sink.payloads)
    assert sink.payloads and all(SENTINEL.encode() not in payload for payload in sink.payloads)
    assert processor_events and all(SENTINEL not in repr(event) for event in processor_events)
    spans = exporter.get_finished_spans()
    assert spans == ()  # The sensitive request was suppressed by the installed OTEL context.

    async def ordinary_provider_request() -> None:
        ordinary_transport = MemoryTransport(Body([b"public"]))
        ordinary_client = httpx.AsyncClient(
            transport=AsyncOpenTelemetryTransport(ordinary_transport, tracer_provider=provider)
        )
        await ordinary_client.get(PUBLIC_URL)
        await ordinary_client.aclose()

    anyio.run(ordinary_provider_request)
    spans = exporter.get_finished_spans()
    assert spans and all(SENTINEL not in repr(span.attributes) + repr(span.events) for span in spans)
    assert any("ordinary.example.invalid" in repr(span.attributes) for span in spans)
    assert owner._sensitive_file_request.get() is False
