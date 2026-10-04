"""Independent boundary checks for the revised SSRF deadline contract."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from types import SimpleNamespace
from typing import override
from unittest.mock import patch

import anyio
import httpx
import pytest

from core.helper import ssrf_proxy

TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


class HangingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        try:
            with anyio.CancelScope(shield=True):
                await anyio.sleep_forever()
                yield b"unreachable"
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    @override
    async def aclose(self) -> None:
        self.closed = True


class DelayedCloseBody(httpx.AsyncByteStream):
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"ok"

    @override
    async def aclose(self) -> None:
        await anyio.sleep(self.delay)
        self.closed = True


class SingleResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: httpx.AsyncByteStream) -> None:
        self.body = body
        self.calls = 0
        self.closed = False

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, stream=self.body, request=request)

    @override
    async def aclose(self) -> None:
        self.closed = True


def test_raw_cancellation_crosses_shield_without_leaving_request_task() -> None:
    async def scenario() -> None:
        body = HangingBody()
        transport = SingleResponseTransport(body)
        client = httpx.AsyncClient(transport=transport)
        task_set_before = asyncio.all_tasks()
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            started = time.monotonic()
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.invalid/body",
                    deadline=started + 0.16,
                    max_response_bytes=32,
                    headers={"traceparent": TRACEPARENT},
                )
        assert time.monotonic() - started < 0.22
        assert body.started.is_set() and body.cancelled and body.closed
        assert transport.calls == 1 and transport.closed and client.is_closed
        assert asyncio.all_tasks() == task_set_before

    anyio.run(scenario)


def test_slow_cleanup_uses_remaining_absolute_budget_and_fails_closed() -> None:
    async def scenario() -> None:
        body = DelayedCloseBody(delay=0.2)
        transport = SingleResponseTransport(body)
        client = httpx.AsyncClient(transport=transport)
        started = time.monotonic()
        deadline = started + 0.3
        with patch.object(ssrf_proxy, "_build_deadline_ssrf_client", return_value=client):
            with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError) as caught:
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.invalid/close",
                    deadline=deadline,
                    max_response_bytes=32,
                )
        elapsed = time.monotonic() - started
        assert caught.value.code == "termination_unconfirmed"
        assert caught.value.termination_confirmed is False
        assert elapsed < 0.35
        assert not body.closed
        assert transport.closed and client.is_closed
        assert asyncio.all_tasks() == {asyncio.current_task()}

    anyio.run(scenario)


def test_sequential_requests_share_deadline_after_first_cleanup() -> None:
    async def scenario() -> None:
        first_body = DelayedCloseBody(delay=0.35)
        first_transport = SingleResponseTransport(first_body)
        first_client = httpx.AsyncClient(transport=first_transport)
        second_body = HangingBody()
        second_transport = SingleResponseTransport(second_body)
        second_client = httpx.AsyncClient(transport=second_transport)
        started = time.monotonic()
        shared_deadline = started + 1.2
        with patch.object(
            ssrf_proxy,
            "_build_deadline_ssrf_client",
            side_effect=[first_client, second_client],
        ) as build:
            response = await ssrf_proxy.async_make_request_with_deadline(
                "GET",
                "https://example.invalid/first",
                deadline=shared_deadline,
                max_response_bytes=32,
            )
            assert response.content == b"ok" and response.is_closed
            with pytest.raises(ssrf_proxy.RequestDeadlineExceededError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.invalid/second",
                    deadline=shared_deadline,
                    max_response_bytes=32,
                )
            elapsed = time.monotonic() - started
        assert elapsed < 1.25
        assert build.call_count == 2
        assert first_transport.calls == second_transport.calls == 1
        assert second_body.started.is_set() and second_body.cancelled
        assert first_client.is_closed and second_client.is_closed

    anyio.run(scenario)


def test_unknown_dependency_versions_fail_closed_before_client_creation() -> None:
    with (
        patch.object(ssrf_proxy, "_DEADLINE_RUNTIME_VERSIONS", ("0.29", "1.0.9", "4.14.1")),
        patch.object(ssrf_proxy.httpx, "AsyncClient") as build,
    ):
        with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError) as caught:
            ssrf_proxy.make_request_with_deadline(
                "GET",
                "https://example.invalid/version",
                deadline=time.monotonic() + 1,
                max_response_bytes=32,
            )
        assert caught.value.termination_confirmed is False
        build.assert_not_called()


def test_unknown_pool_backend_shape_fails_closed_without_success_response() -> None:
    async def scenario() -> None:
        class UnsupportedClient:
            def __init__(self) -> None:
                self._transport = SimpleNamespace(_pool=SimpleNamespace(_network_backend=object()))
                self._mounts: dict[str, object] = {}
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        client = UnsupportedClient()
        with (
            patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_ALL_URL", None),
            patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTP_URL", None),
            patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTPS_URL", None),
            patch.object(ssrf_proxy.httpx, "AsyncClient", return_value=client),
        ):
            with pytest.raises(ssrf_proxy.RequestTerminationUnconfirmedError):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    "https://example.invalid/shape",
                    deadline=time.monotonic() + 1,
                    max_response_bytes=32,
                )
        assert client.closed

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("all_proxy", "http_proxy", "https_proxy", "expected"),
    [
        ("http://all.proxy.invalid:8080", "http://h.proxy.invalid:8080", "http://s.proxy.invalid:8080", "all"),
        (None, "http://h.proxy.invalid:8080", "http://s.proxy.invalid:8080", "mounts"),
        (None, None, "http://s.proxy.invalid:8080", "direct"),
    ],
)
def test_deadline_client_keeps_existing_proxy_precedence_and_fixed_tls(
    all_proxy: str | None, http_proxy: str | None, https_proxy: str | None, expected: str
) -> None:
    with (
        patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_ALL_URL", all_proxy),
        patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTP_URL", http_proxy),
        patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTPS_URL", https_proxy),
        patch.object(ssrf_proxy.httpx, "AsyncClient") as async_client,
        patch.object(ssrf_proxy.httpx, "AsyncHTTPTransport") as transport,
    ):
        ssrf_proxy._build_deadline_ssrf_client()
    options = async_client.call_args.kwargs
    assert options["verify"] is True and options["follow_redirects"] is False
    if expected == "all":
        assert options["proxy"] == all_proxy and "mounts" not in options
        transport.assert_not_called()
    elif expected == "mounts":
        assert set(options["mounts"]) == {"http://", "https://"}
        assert [call.kwargs["proxy"] for call in transport.call_args_list] == [http_proxy, https_proxy]
        assert all(call.kwargs["verify"] is True and call.kwargs["retries"] == 0 for call in transport.call_args_list)
    else:
        assert "proxy" not in options and "mounts" not in options
        transport.assert_not_called()


def test_legacy_sync_api_still_uses_existing_pooled_client() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request)))
    with (
        patch.object(ssrf_proxy, "_get_ssrf_client", return_value=client) as get_client,
        patch.object(ssrf_proxy, "_inject_trace_headers", side_effect=lambda headers: headers),
    ):
        response = ssrf_proxy.make_request("GET", "https://example.invalid/legacy", max_retries=0)
    assert response.status_code == 503
    get_client.assert_called_once_with(True)
    client.close()


@pytest.mark.parametrize("bad_limit", [0, -1, True, 2.5])
def test_invalid_response_limit_is_rejected_before_client_creation(bad_limit: object) -> None:
    with patch.object(ssrf_proxy, "_build_deadline_ssrf_client") as build:
        with pytest.raises(ValueError, match="max_response_bytes"):
            ssrf_proxy.make_request_with_deadline(
                "GET",
                "https://example.invalid/limit",
                deadline=time.monotonic() + 1,
                max_response_bytes=bad_limit,  # type: ignore[arg-type]
            )
        build.assert_not_called()


@pytest.mark.parametrize("mode", ["slow_headers", "tls_stall"])
def test_real_socket_deadline_closes_tls_tcp_fd_and_has_no_late_dispatch(mode: str) -> None:
    async def scenario() -> None:
        disconnected = asyncio.Event()
        requests: list[bytes] = []
        handlers: set[asyncio.Task] = set()
        captured_sockets: list[object] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            handlers.add(task)
            feeder: asyncio.Task | None = None
            try:
                if mode == "tls_stall":
                    hello = await reader.read(65536)
                    assert hello.startswith(b"\x16\x03")
                    requests.append(hello)
                else:
                    requests.append(await reader.readuntil(b"\r\n\r\n"))

                    async def slow_headers() -> None:
                        await asyncio.sleep(5)
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                        await writer.drain()

                    feeder = asyncio.create_task(slow_headers())
                try:
                    with suppress(ConnectionResetError):
                        await reader.read()
                finally:
                    disconnected.set()
            finally:
                if feeder is not None:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            url = f"http{'s' if mode == 'tls_stall' else ''}://127.0.0.1:{port}/"
            original_connect = ssrf_proxy._DeadlineNetworkBackend.connect_tcp

            async def capture_connect(backend, *args, **kwargs):
                stream = await original_connect(backend, *args, **kwargs)
                captured_sockets.append(stream.get_extra_info("socket"))
                return stream

            started = time.monotonic()
            with (
                patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_ALL_URL", None),
                patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTP_URL", None),
                patch.object(ssrf_proxy.dify_config, "SSRF_PROXY_HTTPS_URL", None),
                patch.object(ssrf_proxy._DeadlineNetworkBackend, "connect_tcp", capture_connect),
                pytest.raises(ssrf_proxy.RequestDeadlineExceededError),
            ):
                await ssrf_proxy.async_make_request_with_deadline(
                    "GET",
                    url,
                    deadline=started + 0.32,
                    max_response_bytes=256,
                )
            assert 0.2 < time.monotonic() - started < 0.32
            await asyncio.wait_for(disconnected.wait(), 1)
            assert len(requests) == 1 and captured_sockets
            assert all(sock.fileno() == -1 for sock in captured_sockets)
            await asyncio.wait_for(asyncio.gather(*tuple(handlers)), 1)
            assert not handlers
        finally:
            server.close()
            await server.wait_closed()
            pending = list(handlers)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    anyio.run(scenario)
