"""Cancellation/resource tests plus short-lived real socket deadline fixtures."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import override
from unittest.mock import patch

import anyio
import httpx
import pytest

from core.helper import ssrf_proxy
from core.tools.errors import ToolSSRFError


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, wait: bool = False):
        self.chunks = chunks
        self.wait = wait
        self.closed = False
        self.cancelled = False
        self.entered = asyncio.Event()

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        try:
            if self.wait:
                await anyio.sleep_forever()
            for chunk in self.chunks:
                yield chunk
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    @override
    async def aclose(self) -> None:
        self.closed = True


class TrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: TrackingStream, *, status: int = 200, headers: dict[str, str] | None = None):
        self.stream = stream
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []
        self.closed = False

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers, stream=self.stream, request=request)

    @override
    async def aclose(self) -> None:
        self.closed = True


def test_bounded_success_preserves_trace_host_and_timeouts() -> None:
    async def scenario() -> None:
        stream = TrackingStream([b'{"ok":', b"true}"])
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            response = await ssrf_proxy.async_make_request_with_deadline(
                "GET",
                "https://example.test",
                deadline=time.monotonic() + 45,
                max_response_bytes=256 * 1024,
                headers={"Host": "virtual.example.test", "TraceParent": "existing-trace"},
            )
        assert response.json() == {"ok": True}
        assert response.is_closed and stream.closed and transport.closed and client.is_closed
        request = transport.requests[0]
        assert request.headers["host"] == "virtual.example.test"
        assert request.headers["traceparent"] == "existing-trace"
        assert request.extensions["timeout"] == {"connect": 3.0, "read": 10.0, "write": 10.0, "pool": 10.0}

    anyio.run(scenario)


def test_sync_adapter_returns_closed_response_with_no_status_retry() -> None:
    stream = TrackingStream([b"failure"])
    transport = TrackingTransport(stream, status=503)
    client = httpx.AsyncClient(transport=transport)
    with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
        response = ssrf_proxy.make_request_with_deadline(
            "POST",
            "https://example.test",
            deadline=time.monotonic() + 45,
            max_response_bytes=256,
            json={"fixture": "public"},
            max_retries=0,
            ssl_verify=True,
            follow_redirects=False,
        )
    assert response.status_code == 503 and response.content == b"failure"
    assert len(transport.requests) == 1
    assert stream.closed and transport.closed and client.is_closed


@pytest.mark.parametrize("all_proxy", [None, "http://all.proxy.test:8080"])
def test_deadline_client_reuses_proxy_selection_and_fixed_security(
    all_proxy: str | None,
    config_overrides: Callable[..., None],
) -> None:
    config_overrides(
        SSRF_PROXY_ALL_URL=all_proxy,
        SSRF_PROXY_HTTP_URL="http://http.proxy.test:8080",
        SSRF_PROXY_HTTPS_URL="http://https.proxy.test:8080",
    )
    with patch.object(ssrf_proxy.httpx, "AsyncClient") as client, patch.object(
        ssrf_proxy.httpx, "AsyncHTTPTransport"
    ) as transport:
        ssrf_proxy._build_deadline_ssrf_client()
    options = client.call_args.kwargs
    assert options["verify"] is True and options["follow_redirects"] is False
    assert options["limits"] is ssrf_proxy._SSRF_CLIENT_LIMITS
    if all_proxy:
        assert options["proxy"] == all_proxy and "mounts" not in options
        transport.assert_not_called()
    else:
        assert set(options["mounts"]) == {"http://", "https://"}
        assert [call.kwargs["proxy"] for call in transport.call_args_list] == [
            "http://http.proxy.test:8080",
            "http://https.proxy.test:8080",
        ]
        assert all(call.kwargs["verify"] is True and call.kwargs["retries"] == 0 for call in transport.call_args_list)


@pytest.mark.parametrize("limit", [256 * 1024, 1024 * 1024])
def test_response_limit_closes_without_parsing(limit: int) -> None:
    async def scenario() -> None:
        stream = TrackingStream([b"x" * limit, b"x"])
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            with pytest.raises(ssrf_proxy.ResponseTooLargeError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=time.monotonic() + 45,
                    max_response_bytes=limit,
                )
        assert stream.closed and transport.closed and client.is_closed

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (200, {"content-encoding": "gzip"}, ssrf_proxy.UnsupportedResponseEncodingError),
        (403, {"server": "Squid"}, ToolSSRFError),
    ],
)
def test_rejection_closes_stream(status: int, headers: dict[str, str], error: type[Exception]) -> None:
    async def scenario() -> None:
        stream = TrackingStream([b"unread"])
        transport = TrackingTransport(stream, status=status, headers=headers)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            with pytest.raises(error):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=time.monotonic() + 45,
                    max_response_bytes=256,
                )
        assert not stream.entered.is_set()
        assert stream.closed and transport.closed

    anyio.run(scenario)


def test_caller_cancellation_stops_task_and_closes_resources() -> None:
    async def scenario() -> None:
        stream = TrackingStream([], wait=True)
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            task = asyncio.create_task(
                ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=time.monotonic() + 45,
                    max_response_bytes=256,
                )
            )
            await stream.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert task.done() and stream.cancelled and stream.closed and transport.closed and client.is_closed

    anyio.run(scenario)


def test_expired_shared_deadline_does_not_construct_client() -> None:
    with patch.object(ssrf_proxy, "_build_deadline_ssrf_client") as build:
        with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
            ssrf_proxy.make_request_with_deadline(
                "GET",
                "https://example.test",
                deadline=time.monotonic() - 1,
                max_response_bytes=256,
            )
        build.assert_not_called()


@pytest.mark.parametrize("stage", ["response", "client"])
def test_slow_cleanup_is_bounded_unknown_and_no_background_task(stage: str) -> None:
    async def scenario() -> None:
        class SlowCloseStream(TrackingStream):
            @override
            async def aclose(self) -> None:
                if stage == "response":
                    await anyio.sleep(0.12)
                await super().aclose()

        class SlowCloseTransport(TrackingTransport):
            @override
            async def aclose(self) -> None:
                if stage == "client":
                    await anyio.sleep(0.12)
                await super().aclose()

        stream = SlowCloseStream([b"{}"])
        transport = SlowCloseTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        before = asyncio.all_tasks()
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            started = time.monotonic()
            with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError) as exc:
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=started + 0.04,
                    max_response_bytes=256,
                )
            assert time.monotonic() - started < 0.08
        assert exc.value.code == "termination_unconfirmed" and exc.value.termination_confirmed is False
        assert asyncio.all_tasks() == before
        if stage == "response":
            assert not stream.closed and transport.closed
        else:
            assert stream.closed and not transport.closed

    anyio.run(scenario)


def test_raw_deadline_cancellation_interrupts_nested_anyio_shield() -> None:
    async def scenario() -> None:
        class ShieldedStream(TrackingStream):
            @override
            async def __aiter__(self) -> AsyncIterator[bytes]:
                with anyio.CancelScope(shield=True):
                    async for chunk in super().__aiter__():
                        yield chunk

        stream = ShieldedStream([], wait=True)
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            started = time.monotonic()
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=started + 0.06,
                    max_response_bytes=256,
                )
        assert time.monotonic() - started < 0.1
        assert stream.cancelled and stream.closed and transport.closed

    anyio.run(scenario)


def test_setup_consuming_shared_budget_never_dispatches() -> None:
    async def scenario() -> None:
        stream = TrackingStream([b"{}"])
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)

        def build() -> httpx.AsyncClient:
            time.sleep(0.05)
            return client

        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", side_effect=build):
            with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=time.monotonic() + 0.04,
                    max_response_bytes=256,
                )
        assert transport.requests == []
        await client.aclose()  # Test-only mock resource cleanup, never a dispatched request.

    anyio.run(scenario)


def test_unverified_transport_runtime_refuses_client_construction() -> None:
    with patch.object(ssrf_proxy, "_DEADLINE_RUNTIME_VERSIONS", ("future", "future", "future")), patch.object(
        ssrf_proxy.httpx, "AsyncClient"
    ) as build:
        with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError):
            ssrf_proxy.make_request_with_deadline(
                "GET",
                "https://example.test",
                deadline=time.monotonic() + 45,
                max_response_bytes=256,
            )
        build.assert_not_called()


def test_close_exception_is_unknown_never_success_and_client_still_closes() -> None:
    async def scenario() -> None:
        class FailingStream(TrackingStream):
            @override
            async def aclose(self) -> None:
                raise RuntimeError("fixture close failed")

        stream = FailingStream([b"{}"])
        transport = TrackingTransport(stream)
        client = httpx.AsyncClient(transport=transport)
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test",
                    deadline=time.monotonic() + 45,
                    max_response_bytes=256,
                )
        assert not stream.closed and transport.closed and client.is_closed

    anyio.run(scenario)


def test_multiple_requests_share_one_absolute_budget_including_cleanup() -> None:
    async def scenario() -> None:
        class DelayedStream(TrackingStream):
            @override
            async def __aiter__(self) -> AsyncIterator[bytes]:
                await anyio.sleep(0.03)
                async for chunk in super().__aiter__():
                    yield chunk

        first = TrackingTransport(DelayedStream([b"{}"]))
        second = TrackingTransport(TrackingStream([], wait=True))
        clients = [httpx.AsyncClient(transport=first), httpx.AsyncClient(transport=second)]
        started = time.monotonic()
        shared_deadline = started + 0.08
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", side_effect=clients) as build:
            await ssrf_proxy.async_make_request_with_deadline(
                "GET",
                "https://example.test/first",
                deadline=shared_deadline,
                max_response_bytes=256,
            )
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test/second",
                    deadline=shared_deadline,
                    max_response_bytes=256,
                )
            assert time.monotonic() - started < 0.1
            await anyio.sleep(0.03)
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.test/third",
                    deadline=shared_deadline,
                    max_response_bytes=256,
                )
        assert build.call_count == 2 and first.closed and second.closed
        assert len(first.requests) == len(second.requests) == 1

    anyio.run(scenario)


@pytest.mark.parametrize(
    "options",
    [
        {"ssl_verify": False},
        {"max_retries": 1},
        {"follow_redirects": True},
        {"allow_redirects": True},
        {"timeout": None},
        {"stream_response": True},
        {"extensions": {"timeout": {"read": 1000}}},
        {"request_timeout": 15.01},
    ],
)
def test_policy_cannot_be_relaxed(options: dict[str, object]) -> None:
    with patch.object(ssrf_proxy, "_build_deadline_ssrf_client") as build:
        with pytest.raises(ValueError):
            ssrf_proxy.make_request_with_deadline(
                "GET",
                "https://example.test",
                deadline=time.monotonic() + 45,
                max_response_bytes=256,
                **options,
            )
        build.assert_not_called()


@asynccontextmanager
async def socket_fixture(mode: str):
    """Run only during one test, recording remote EOF/RST before fixture shutdown."""
    disconnected = asyncio.Event()
    requests: list[bytes] = []
    handlers: set[asyncio.Task] = set()

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)

        async def feed() -> None:
            if mode == "slow_headers":
                await asyncio.sleep(5)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
            elif mode == "drip":
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                while True:
                    writer.write(b"1\r\nx\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.03)
            else:
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: /followed\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()

        feeder: asyncio.Task | None = None
        try:
            if mode == "tls_stall":
                hello = await reader.read(65536)
                assert hello.startswith(b"\x16\x03")
                requests.append(hello)
            else:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                feeder = asyncio.create_task(feed())
            try:
                assert await reader.read() == b""
            except ConnectionResetError:
                # Closing a socket with a concurrently arriving drip can send RST.
                pass
            disconnected.set()
        finally:
            if feeder is not None:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(connection, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/", disconnected, requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("mode", ["slow_headers", "drip", "tls_stall"])
@pytest.mark.parametrize("short_budget", ["shared_deadline", "request_timeout"])
def test_socket_deadline_cancels_and_remote_observes_disconnect(
    mode: str,
    short_budget: str,
    config_overrides: Callable[..., None],
) -> None:
    config_overrides(SSRF_PROXY_ALL_URL=None, SSRF_PROXY_HTTP_URL=None, SSRF_PROXY_HTTPS_URL=None)

    async def scenario() -> None:
        async with socket_fixture(mode) as (url, disconnected, requests):
            if mode == "tls_stall":
                url = url.replace("http://", "https://", 1)
            started = time.monotonic()
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    url,
                    deadline=started + (0.35 if short_budget == "shared_deadline" else 45),
                    request_timeout=0.35 if short_budget == "request_timeout" else 15,
                    max_response_bytes=256 * 1024,
                )
            elapsed = time.monotonic() - started
            assert 0.25 < elapsed < 0.35
            await asyncio.wait_for(disconnected.wait(), 1)
            assert len(requests) == 1

    anyio.run(scenario)


def test_socket_redirect_is_not_followed(config_overrides: Callable[..., None]) -> None:
    config_overrides(SSRF_PROXY_ALL_URL=None, SSRF_PROXY_HTTP_URL=None, SSRF_PROXY_HTTPS_URL=None)

    async def scenario() -> None:
        async with socket_fixture("redirect") as (url, disconnected, requests):
            response = await ssrf_proxy.async_make_request_with_deadline(
                "GET",
                url,
                deadline=time.monotonic() + 45,
                max_response_bytes=256,
            )
            assert response.status_code == 302 and response.content == b"{}"
            await asyncio.wait_for(disconnected.wait(), 1)
            assert len(requests) == 1

    anyio.run(scenario)
