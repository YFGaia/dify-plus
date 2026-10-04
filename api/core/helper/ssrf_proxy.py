"""SSRF-protected HTTP client for generic outbound requests.

Use this module when the URL represents a normal external HTTP interaction that
must go through network/proxy policy exactly as requested, such as HTTP Request
nodes, provider/API integrations, auth discovery, or custom tool calls.

Do not use this directly for "remote file" retrieval. File downloads, probes,
and metadata checks should use `core.file.remote_fetcher` instead so Dify-signed
file URLs can be resolved through DB + storage before falling back to this SSRF
client.
"""

import asyncio
import logging
import math
import time
from functools import partial
from importlib.metadata import version
from typing import Any

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.auto import AutoBackend
from pydantic import TypeAdapter, ValidationError

from configs import dify_config
from core.helper.http_client_pooling import get_pooled_http_client
from core.tools.errors import ToolSSRFError
from graphon.http.response import HttpResponse

logger = logging.getLogger(__name__)

SSRF_DEFAULT_MAX_RETRIES = dify_config.SSRF_DEFAULT_MAX_RETRIES

BACKOFF_FACTOR = 0.5
STATUS_FORCELIST = [429, 500, 502, 503, 504]

type Headers = dict[str, str]
_HEADERS_ADAPTER: TypeAdapter[Headers] = TypeAdapter(Headers)

_SSL_VERIFIED_POOL_KEY = "ssrf:verified"
_SSL_UNVERIFIED_POOL_KEY = "ssrf:unverified"
_SSRF_CLIENT_LIMITS = httpx.Limits(
    max_connections=dify_config.SSRF_POOL_MAX_CONNECTIONS,
    max_keepalive_connections=dify_config.SSRF_POOL_MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=dify_config.SSRF_POOL_KEEPALIVE_EXPIRY,
)
_DEADLINE_RUNTIME_VERSIONS = (httpx.__version__, httpcore.__version__, version("anyio"))


class MaxRetriesExceededError(ValueError):
    """Raised when the maximum number of retries is exceeded."""

    pass


class ResponseLimitError(ValueError):
    """Base error for responses that cannot be safely bounded."""

    pass


class ResponseTooLargeError(ResponseLimitError):
    """Raised when an identity response exceeds the configured byte limit."""

    pass


class UnsupportedResponseEncodingError(ResponseLimitError):
    """Raised when response encoding prevents safe decoded-size enforcement."""

    pass


class RequestDeadlineExceededError(TimeoutError):
    """A bounded request was cancelled at its absolute monotonic deadline."""


class RequestTerminationUnconfirmedError(RequestDeadlineExceededError):
    """Client resource termination could not be confirmed; callers must fail closed.

    Do not issue another request or mint a session in the same operation. This
    describes client resources, never proof of remote business termination.
    """

    code = "termination_unconfirmed"
    termination_confirmed = False


class _DeadlineNetworkBackend(httpcore.AsyncNetworkBackend):
    """Observe sockets without changing the existing backend's network policy."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend, streams: list[httpcore.AsyncNetworkStream]):
        self.backend = backend
        self.streams = streams

    async def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        stream = await self.backend.connect_tcp(*args, **kwargs)
        self.streams.append(stream)
        if not isinstance(stream, AnyIOStream) or not isinstance(
            getattr(getattr(stream, "_stream", None), "_transport", None), asyncio.Transport
        ):
            raise RequestTerminationUnconfirmedError("unsupported network stream termination adapter")
        return stream

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        stream = await self.backend.connect_unix_socket(*args, **kwargs)
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


request_error = httpx.RequestError
max_retries_exceeded_error = MaxRetriesExceededError


def _create_proxy_mounts(verify: bool) -> dict[str, httpx.HTTPTransport]:
    """Build per-scheme proxy transports with the same TLS policy as the SSRF client."""
    return {
        "http://": httpx.HTTPTransport(
            proxy=dify_config.SSRF_PROXY_HTTP_URL,
            verify=verify,
        ),
        "https://": httpx.HTTPTransport(
            proxy=dify_config.SSRF_PROXY_HTTPS_URL,
            verify=verify,
        ),
    }


def _build_ssrf_client(verify: bool) -> httpx.Client:
    if dify_config.SSRF_PROXY_ALL_URL:
        return httpx.Client(
            proxy=dify_config.SSRF_PROXY_ALL_URL,
            verify=verify,
            limits=_SSRF_CLIENT_LIMITS,
        )

    if dify_config.SSRF_PROXY_HTTP_URL and dify_config.SSRF_PROXY_HTTPS_URL:
        return httpx.Client(
            mounts=_create_proxy_mounts(verify=verify),
            verify=verify,
            limits=_SSRF_CLIENT_LIMITS,
        )

    return httpx.Client(verify=verify, limits=_SSRF_CLIENT_LIMITS)


def _get_ssrf_client(ssl_verify_enabled: bool) -> httpx.Client:
    if not isinstance(ssl_verify_enabled, bool):
        raise ValueError("SSRF client verify flag must be a boolean")

    return get_pooled_http_client(
        _SSL_VERIFIED_POOL_KEY if ssl_verify_enabled else _SSL_UNVERIFIED_POOL_KEY,
        lambda: _build_ssrf_client(verify=ssl_verify_enabled),
    )


def _get_user_provided_host_header(headers: Headers | None) -> str | None:
    """
    Extract the user-provided Host header from the headers dict.

    This is needed because when using a forward proxy, httpx may override the Host header.
    We preserve the user's explicit Host header to support virtual hosting and other use cases.
    """
    if not headers:
        return None
    # Case-insensitive lookup for Host header
    for key, value in headers.items():
        if key.lower() == "host":
            return value
    return None


def _inject_trace_headers(headers: Headers | None) -> Headers:
    """
    Inject W3C traceparent header for distributed tracing.

    When OTEL is enabled, HTTPXClientInstrumentor handles trace propagation automatically.
    When OTEL is disabled, we manually inject the traceparent header.
    """
    if headers is None:
        headers = {}

    # Skip if already present (case-insensitive check)
    for key in headers:
        if key.lower() == "traceparent":
            return headers

    # Skip if OTEL is enabled - HTTPXClientInstrumentor handles this automatically
    if dify_config.ENABLE_OTEL:
        return headers

    # Generate and inject traceparent for non-OTEL scenarios
    try:
        from core.helper.trace_id_helper import generate_traceparent_header

        traceparent = generate_traceparent_header()
        if traceparent:
            headers["traceparent"] = traceparent
    except Exception:
        # Silently ignore errors to avoid breaking requests
        logger.debug("Failed to generate traceparent header", exc_info=True)

    return headers


def make_request(
    method: str,
    url: str,
    max_retries: int = SSRF_DEFAULT_MAX_RETRIES,
    stream_response: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    """Send one SSRF-protected request with optional streaming.

    Args:
        method: HTTP method sent through the configured SSRF client.
        url: Absolute request URL.
        max_retries: Number of retry attempts after the initial request.
        stream_response: Return an open streaming response that the caller must close.
        **kwargs: Additional keyword arguments forwarded to ``httpx.Client``.

    Returns:
        A buffered response, or an open response when ``stream_response`` is true.

    Raises:
        ToolSSRFError: The configured SSRF proxy rejects the destination.
        MaxRetriesExceededError: All configured request attempts fail.
        httpx.RequestError: A request fails while retries are disabled.
        ValueError: The SSL verification option or request headers are invalid.
    """
    # Convert requests-style allow_redirects to httpx-style follow_redirects
    if "allow_redirects" in kwargs:
        allow_redirects = kwargs.pop("allow_redirects")
        if "follow_redirects" not in kwargs:
            kwargs["follow_redirects"] = allow_redirects

    if "timeout" not in kwargs:
        kwargs["timeout"] = httpx.Timeout(
            timeout=dify_config.SSRF_DEFAULT_TIME_OUT,
            connect=dify_config.SSRF_DEFAULT_CONNECT_TIME_OUT,
            read=dify_config.SSRF_DEFAULT_READ_TIME_OUT,
            write=dify_config.SSRF_DEFAULT_WRITE_TIME_OUT,
        )

    # prioritize per-call option, which can be switched on and off inside the HTTP node on the web UI
    verify_option = kwargs.pop("ssl_verify", dify_config.HTTP_REQUEST_NODE_SSL_VERIFY)
    if not isinstance(verify_option, bool):
        raise ValueError("ssl_verify must be a boolean")
    client = _get_ssrf_client(verify_option)

    # Inject traceparent header for distributed tracing (when OTEL is not enabled)
    try:
        headers: Headers = _HEADERS_ADAPTER.validate_python(kwargs.get("headers") or {})
    except ValidationError as e:
        raise ValueError("headers must be a mapping of string keys to string values") from e
    headers = _inject_trace_headers(headers)
    kwargs["headers"] = headers

    # Preserve user-provided Host header
    # When using a forward proxy, httpx may override the Host header based on the URL.
    # We extract and preserve any explicitly set Host header to support virtual hosting.
    user_provided_host = _get_user_provided_host_header(headers)
    send_kwargs: dict[str, Any] = {}
    if "auth" in kwargs:
        send_kwargs["auth"] = kwargs.pop("auth")
    if "follow_redirects" in kwargs:
        send_kwargs["follow_redirects"] = kwargs.pop("follow_redirects")

    retries = 0
    while retries <= max_retries:
        try:
            # Preserve the user-provided Host header
            # httpx may override the Host header when using a proxy
            headers = {k: v for k, v in headers.items() if k.lower() != "host"}
            if user_provided_host is not None:
                headers["host"] = user_provided_host
            kwargs["headers"] = headers
            request = client.build_request(method=method, url=url, **kwargs)
            if stream_response:
                response = client.send(request, stream=True, **send_kwargs)
            else:
                response = client.send(request, **send_kwargs)

            # Check for SSRF protection by Squid proxy
            if response.status_code in (401, 403):
                # Check if this is a Squid SSRF rejection
                server_header = response.headers.get("server", "").lower()
                via_header = response.headers.get("via", "").lower()

                # Squid typically identifies itself in Server or Via headers
                if "squid" in server_header or "squid" in via_header:
                    # The deny ACL is usually ``to_private_networks`` (RFC1918 +
                    # loopback / link-local / CGN / IPv6 ULA, etc.). We don't know
                    # which specific ACL tripped from Squid's response alone, but
                    # the actionable remediation is the same in every case:
                    # allowlist the destination in the SSRF proxy. Tell the user
                    # exactly which env var to set so they don't have to grep the
                    # squid config. Mention a concrete example CIDR (e.g. the
                    # 172.21.0.0/16 from the bug report) so they can copy-paste it.
                    response.close()
                    raise ToolSSRFError(
                        f"Access to '{url}' was blocked by SSRF protection "
                        f"(e.g. SSRF_PROXY_ALLOW_PRIVATE_IPS=172.21.0.0/16 to "
                        f"allow 172.21.0.0/16). The URL resolves to a private, "
                        f"loopback, link-local, or otherwise non-public network "
                        f"address. See https://github.com/langgenius/dify/issues/38443."
                    )

            if response.status_code not in STATUS_FORCELIST or max_retries == 0:
                return response
            else:
                logger.warning(
                    "Received status code %s for URL %s which is in the force list",
                    response.status_code,
                    url,
                )
                response.close()

        except httpx.RequestError as e:
            logger.warning("Request to URL %s failed on attempt %s: %s", url, retries + 1, e)
            if max_retries == 0:
                raise

        retries += 1
        if retries <= max_retries:
            time.sleep(BACKOFF_FACTOR * (2 ** (retries - 1)))
    raise MaxRetriesExceededError(f"Reached maximum retries ({max_retries}) for URL {url}")


def buffer_response(response: httpx.Response, *, max_response_bytes: int) -> httpx.Response:
    """Consume one open identity response under a decoded byte limit and close its stream."""
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")

    try:
        content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise UnsupportedResponseEncodingError(f"content encoding {content_encoding} cannot be safely bounded")
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > max_response_bytes:
                raise ResponseTooLargeError(f"response exceeded {max_response_bytes} bytes")
            content.extend(chunk)
        decoded_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        try:
            request = response.request
        except RuntimeError:
            request = None
        return httpx.Response(
            response.status_code,
            headers=decoded_headers,
            content=bytes(content),
            request=request,
            extensions=response.extensions,
            history=response.history,
            default_encoding=response.default_encoding,
        )
    finally:
        response.close()


def _build_deadline_ssrf_client() -> httpx.AsyncClient:
    """Use the existing SSRF proxy selection with verified, non-retrying transports.

    A request owns its async client so cancellation can close all its connections;
    async clients must not be shared across the synchronous adapter's event loops.
    This does not change the legacy client's pooling, headers or proxy defaults.
    """
    if _DEADLINE_RUNTIME_VERSIONS != ("0.28.1", "1.0.9", "4.14.1"):
        raise RequestTerminationUnconfirmedError("deadline transport runtime requires verification")
    options: dict[str, Any] = {"verify": True, "follow_redirects": False, "limits": _SSRF_CLIENT_LIMITS}
    if dify_config.SSRF_PROXY_ALL_URL:
        options["proxy"] = dify_config.SSRF_PROXY_ALL_URL
    elif dify_config.SSRF_PROXY_HTTP_URL and dify_config.SSRF_PROXY_HTTPS_URL:
        options["mounts"] = {
            scheme: httpx.AsyncHTTPTransport(proxy=proxy, verify=True, retries=0, limits=_SSRF_CLIENT_LIMITS)
            for scheme, proxy in (
                ("http://", dify_config.SSRF_PROXY_HTTP_URL),
                ("https://", dify_config.SSRF_PROXY_HTTPS_URL),
            )
        }
    client = httpx.AsyncClient(**options)
    streams: list[httpcore.AsyncNetworkStream] = []
    # HTTPX 0.28 / HTTPCore owner boundary: partial TLS setup is not yet in the
    # pool's connection object. Track the original TCP stream before start_tls.
    for transport in [client._transport, *client._mounts.values()]:
        if transport is None:
            continue
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if isinstance(backend, AutoBackend | AnyIOBackend):
            pool._network_backend = _DeadlineNetworkBackend(backend, streams)
        else:
            client.__dict__["_deadline_backend_supported"] = False
    client.__dict__["_deadline_network_streams"] = streams
    return client


async def _close_deadline_resources(
    response: httpx.Response | None, client: httpx.AsyncClient | None, *, expires_at: float
) -> None:
    """Abort standard sockets synchronously, then close within the same budget.

    AnyIO's asyncio SocketStream owns an asyncio transport. Aborting it also
    covers TCP streams stranded during a cancelled TLS handshake, before
    HTTPCore has stored its HTTP connection. Unsupported backend shapes fail
    closed rather than claiming termination. No background cleanup task exists.
    """
    failures: list[BaseException] = []
    sockets: list[Any] = []
    for stream in getattr(client, "_deadline_network_streams", []):
        transport = getattr(getattr(stream, "_stream", None), "_transport", None)
        if not isinstance(transport, asyncio.Transport):
            failures.append(RuntimeError("unsupported socket termination adapter"))
            continue
        try:
            raw_socket = stream.get_extra_info("socket")
            if raw_socket is None:
                failures.append(RuntimeError("socket close proof is unavailable"))
            else:
                sockets.append(raw_socket)
            transport.abort()
            if not transport.is_closing():
                failures.append(RuntimeError("socket abort was not confirmed"))
        except Exception as e:
            failures.append(e)
    if sockets:
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            failures.append(TimeoutError("socket close proof budget exhausted"))
        else:
            try:
                async with asyncio.timeout(remaining):
                    # asyncio abort queues connection_lost, which closes the FD.
                    await asyncio.sleep(0)
                if any(raw_socket.fileno() != -1 for raw_socket in sockets):
                    failures.append(RuntimeError("socket descriptor close was not confirmed"))
            except (Exception, asyncio.CancelledError) as e:
                failures.append(e)
    resources = [resource for resource in (response, client) if resource is not None]
    loop = asyncio.get_running_loop()
    for index, resource in enumerate(resources):
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            failures.append(TimeoutError("resource close budget exhausted"))
            continue
        # Preserve a client-close window if response-close stalls.
        close_budget = remaining / (len(resources) - index)
        try:
            async with asyncio.timeout_at(loop.time() + close_budget):
                await resource.aclose()
            if time.monotonic() >= expires_at:
                failures.append(TimeoutError("resource close exceeded deadline"))
        except (Exception, asyncio.CancelledError) as e:
            failures.append(e)
    if failures:
        raise RequestTerminationUnconfirmedError("client request termination is unconfirmed") from failures[0]


async def async_make_request_with_deadline(
    method: str,
    url: str,
    *,
    deadline: float,
    max_response_bytes: int,
    request_timeout: float = 15.0,
    **kwargs: Any,
) -> httpx.Response:
    """Cancel and close one bounded SSRF request before returning buffered bytes.

    ``deadline`` is an absolute ``time.monotonic()`` timestamp shared by the
    caller's whole operation (e.g. callback start + 45 seconds). Each request
    additionally has at most 15 seconds, including response and resource cleanup.
    Connect is capped at 3 seconds and read/write/pool at 10 seconds. Unlike an
    inactivity timeout, the cancellation scope stops an indefinitely dripping
    response. Cancellation also propagates from an enclosing async caller.

    TLS verification, zero retries and no redirects are fixed. Existing trace
    and explicit Host header handling are reused. Only identity encoding is
    accepted, bytes are bounded before JSON parsing, and all response/client
    resources are closed on success, rejection, timeout or caller cancellation.
    This API supports the asyncio backend only. It reserves up to 250ms (20%
    of short budgets) for cleanup, aborts observed standard sockets and awaits
    close within the same absolute budget. Unconfirmed cleanup fails closed;
    callers must stop the whole operation rather than dispatch or mint sessions.
    No request or cleanup runs in a background task after this function returns.
    Synchronous setup cannot be preempted, so budget is checked again before
    dispatch and before returning any successful response.
    """
    started_at = time.monotonic()
    if not math.isfinite(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if not math.isfinite(request_timeout) or not 0 < request_timeout <= 15:
        raise ValueError("request_timeout must be positive and at most 15 seconds")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    expires_at = min(deadline, started_at + request_timeout)
    if expires_at <= started_at:
        raise RequestDeadlineExceededError("request deadline has expired")
    for option, required in (
        ("max_retries", 0),
        ("ssl_verify", True),
        ("follow_redirects", False),
        ("allow_redirects", False),
    ):
        if option in kwargs and (type(kwargs[option]) is not type(required) or kwargs.pop(option) != required):
            raise ValueError(f"{option} cannot override the bounded request policy")
    for option in ("timeout", "stream_response", "verify", "extensions"):
        if option in kwargs:
            raise ValueError(f"{option} cannot override the bounded request policy")
    try:
        headers = _HEADERS_ADAPTER.validate_python(kwargs.get("headers") or {})
    except ValidationError as e:
        raise ValueError("headers must be a mapping of string keys to string values") from e
    headers = _inject_trace_headers(headers)
    host = _get_user_provided_host_header(headers)
    headers = {key: value for key, value in headers.items() if key.lower() != "host"}
    if host is not None:
        headers["host"] = host
    kwargs["headers"] = headers
    auth = kwargs.pop("auth", httpx.USE_CLIENT_DEFAULT)
    response: httpx.Response | None = None
    client: httpx.AsyncClient | None = None
    loop = asyncio.get_running_loop()
    io_expires_at = expires_at - min(0.25, (expires_at - started_at) * 0.2)
    try:
        try:
            async with asyncio.timeout_at(loop.time() + max(0, io_expires_at - time.monotonic())):
                client = _build_deadline_ssrf_client()
                if client.__dict__.get("_deadline_backend_supported") is False:
                    raise RequestTerminationUnconfirmedError("unsupported client termination adapter")
                remaining = io_expires_at - time.monotonic()
                if remaining <= 0:
                    raise RequestDeadlineExceededError("request deadline has expired")
                kwargs["timeout"] = httpx.Timeout(
                    connect=min(3.0, remaining),
                    read=min(10.0, remaining),
                    write=min(10.0, remaining),
                    pool=min(10.0, remaining),
                )
                request = client.build_request(method, url, **kwargs)
                if time.monotonic() >= io_expires_at:
                    raise RequestDeadlineExceededError("request deadline has expired before dispatch")
                response = await client.send(request, stream=True, auth=auth, follow_redirects=False)
                if response.status_code in (401, 403) and any(
                    "squid" in response.headers.get(name, "").lower() for name in ("server", "via")
                ):
                    raise ToolSSRFError("Destination was blocked by the configured SSRF protection")
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in {"", "identity"}:
                    raise UnsupportedResponseEncodingError("response encoding cannot be safely bounded")
                content = bytearray()
                if not isinstance(response.stream, httpx.AsyncByteStream):
                    raise RequestTerminationUnconfirmedError("unsupported response stream adapter")
                # Direct raw stream iteration avoids aiter_raw's implicit close
                # outside our separately budgeted and tracked cleanup phase.
                async for chunk in response.stream:
                    if len(content) + len(chunk) > max_response_bytes:
                        raise ResponseTooLargeError(f"response exceeded {max_response_bytes} bytes")
                    content.extend(chunk)
                buffered = httpx.Response(
                    response.status_code,
                    headers={
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
                    },
                    content=bytes(content),
                    request=request,
                    extensions=response.extensions,
                    default_encoding=response.default_encoding,
                )
        except RequestTerminationUnconfirmedError:
            raise
        except TimeoutError as e:
            raise RequestDeadlineExceededError("request deadline exceeded") from e
    finally:
        await _close_deadline_resources(response, client, expires_at=expires_at)
    if time.monotonic() >= expires_at:
        raise RequestDeadlineExceededError("request deadline exceeded before returning response")
    return buffered


def make_request_with_deadline(
    method: str,
    url: str,
    *,
    deadline: float,
    max_response_bytes: int,
    request_timeout: float = 15.0,
    **kwargs: Any,
) -> httpx.Response:
    """Synchronous adapter; async callers must await the async API instead.

    Runs cancellation and cleanup to completion in this thread. It does not use
    a background worker or return while a timed-out request is still executing.
    """
    return anyio.run(
        partial(
            async_make_request_with_deadline,
            method,
            url,
            deadline=deadline,
            max_response_bytes=max_response_bytes,
            request_timeout=request_timeout,
            **kwargs,
        )
    )


def get(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("GET", url, max_retries=max_retries, **kwargs)


def post(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("POST", url, max_retries=max_retries, **kwargs)


def put(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("PUT", url, max_retries=max_retries, **kwargs)


def patch(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("PATCH", url, max_retries=max_retries, **kwargs)


def delete(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("DELETE", url, max_retries=max_retries, **kwargs)


def head(url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    return make_request("HEAD", url, max_retries=max_retries, **kwargs)


class SSRFProxy:
    """
    Adapter exposing SSRF-protected HTTP helpers behind HttpClientProtocol.

    This is intentionally a thin wrapper over the existing module-level functions so callers can inject it
    where a protocol-typed HTTP client is expected.
    """

    @property
    def max_retries_exceeded_error(self) -> type[Exception]:
        return max_retries_exceeded_error

    @property
    def request_error(self) -> type[Exception]:
        return request_error

    def get(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return get(url=url, max_retries=max_retries, **kwargs)

    def head(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return head(url=url, max_retries=max_retries, **kwargs)

    def post(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return post(url=url, max_retries=max_retries, **kwargs)

    def put(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return put(url=url, max_retries=max_retries, **kwargs)

    def delete(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return delete(url=url, max_retries=max_retries, **kwargs)

    def patch(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
        return patch(url=url, max_retries=max_retries, **kwargs)


def _to_graphon_http_response(response: httpx.Response) -> HttpResponse:
    """Convert an ``httpx`` response into Graphon's transport-agnostic wrapper."""
    return HttpResponse(
        status_code=response.status_code,
        headers=dict(response.headers),
        content=response.content,
        url=str(response.url) if response.url else None,
        reason_phrase=response.reason_phrase,
        fallback_text=response.text,
    )


class GraphonSSRFProxy:
    """Adapter exposing SSRF helpers behind Graphon's ``HttpClientProtocol``."""

    @property
    def max_retries_exceeded_error(self) -> type[Exception]:
        return max_retries_exceeded_error

    @property
    def request_error(self) -> type[Exception]:
        return request_error

    def get(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(get(url=url, max_retries=max_retries, **kwargs))

    def head(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(head(url=url, max_retries=max_retries, **kwargs))

    def post(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(post(url=url, max_retries=max_retries, **kwargs))

    def put(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(put(url=url, max_retries=max_retries, **kwargs))

    def delete(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(delete(url=url, max_retries=max_retries, **kwargs))

    def patch(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> HttpResponse:
        return _to_graphon_http_response(patch(url=url, max_retries=max_retries, **kwargs))


ssrf_proxy = SSRFProxy()
graphon_ssrf_proxy = GraphonSSRFProxy()
