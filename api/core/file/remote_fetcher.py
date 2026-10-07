"""Unified remote-file retrieval with Dify signed file URL resolution.

Use this module for backend workflows whose intent is to fetch remote file content
or remote file metadata from a URL, even when the URL originally came from a user
upload, a workflow variable, a tool/datasource file, or an app DSL. GET/HEAD
requests can resolve Dify-signed file URLs locally through DB + storage before
falling back to the SSRF-protected network client.

Use `core.helper.ssrf_proxy` directly only for generic outbound HTTP where the
URL is not being treated as a remote file, such as HTTP Request nodes, external
API integrations, auth discovery, or user-configured tool calls. Those calls must
stay as real network requests and should not reinterpret Dify file URLs as stored
files.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any, Literal

import httpx

from configs import dify_config
from core.app.file_access import DatabaseFileAccessController
from core.db.session_factory import session_factory
from core.helper import ssrf_proxy
from core.helper.ssrf_proxy import (
    SSRF_DEFAULT_MAX_RETRIES,
    _to_graphon_http_response,
    max_retries_exceeded_error,
    request_error,
)
from extensions.ext_storage import storage
from models import ToolFile, UploadFile

_UPLOAD_FILE_PATH_PATTERN = re.compile(
    r"^/files/(?P<file_id>[a-fA-F0-9-]+)/(?P<preview_kind>file-preview|image-preview)$"
)
_TOOL_FILE_PATH_PATTERN = re.compile(r"^/files/tools/(?P<file_id>[a-fA-F0-9-]+)(?P<extension>\.[^/]*)?$")
_DATASOURCE_FILE_PATH_PATTERN = re.compile(r"^/files/datasources/(?P<file_id>[a-fA-F0-9-]+)(?P<extension>\.[^/]*)?$")

_file_access_controller = DatabaseFileAccessController()


@dataclass(frozen=True)
class _SignedFileUrl:
    file_id: str
    preview_kind: Literal["file-preview", "image-preview"]
    record_kind: Literal["upload", "tool", "datasource"]


def make_request(method: str, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any) -> httpx.Response:
    """Fetch remote file content or metadata.

    GET and HEAD requests for Dify-owned signed file URLs are served from local
    storage. Every other request is delegated unchanged to the SSRF proxy.
    """

    normalized_method = method.upper()
    if normalized_method == "GET":
        response = _resolve_dify_signed_file_url("GET", url)
        if response is not None:
            return response
    if normalized_method == "HEAD":
        response = _resolve_dify_signed_file_url("HEAD", url)
        if response is not None:
            return response
    return ssrf_proxy.make_request(method=method, url=url, max_retries=max_retries, **kwargs)


class GraphonRemoteFileFetcher:
    """Graphon HTTP-client adapter backed by the unified remote-file fetcher.

    Graphon requires method-specific HTTP client methods, while regular Dify
    call sites should use `make_request` directly.
    """

    @property
    def max_retries_exceeded_error(self) -> type[Exception]:
        return max_retries_exceeded_error

    @property
    def request_error(self) -> type[Exception]:
        return request_error

    def get(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("GET", url=url, max_retries=max_retries, **kwargs))

    def head(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("HEAD", url=url, max_retries=max_retries, **kwargs))

    def post(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("POST", url=url, max_retries=max_retries, **kwargs))

    def put(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("PUT", url=url, max_retries=max_retries, **kwargs))

    def delete(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("DELETE", url=url, max_retries=max_retries, **kwargs))

    def patch(self, url: str, max_retries: int = SSRF_DEFAULT_MAX_RETRIES, **kwargs: Any):
        return _to_graphon_http_response(make_request("PATCH", url=url, max_retries=max_retries, **kwargs))


def _resolve_dify_signed_file_url(method: Literal["GET", "HEAD"], url: str) -> httpx.Response | None:
    parsed_url = urllib.parse.urlparse(url)
    if not _is_dify_file_origin(parsed_url):
        return None

    signed_file_url = _parse_signed_file_path(parsed_url.path)
    if signed_file_url is None:
        return None

    query = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
    timestamp = _single_query_value(query, "timestamp")
    nonce = _single_query_value(query, "nonce")
    sign = _single_query_value(query, "sign")
    if timestamp is None or nonce is None or sign is None:
        return None

    if not _verify_signed_file_url(
        signed_file_url=signed_file_url,
        timestamp=timestamp,
        nonce=nonce,
        sign=sign,
    ):
        return None

    if signed_file_url.record_kind == "upload":
        return _build_upload_file_response(method=method, url=url, file_id=signed_file_url.file_id)
    if signed_file_url.record_kind == "tool":
        return _build_tool_file_response(method=method, url=url, file_id=signed_file_url.file_id)
    return _build_datasource_file_response(method=method, url=url, file_id=signed_file_url.file_id)


def _parse_signed_file_path(path: str) -> _SignedFileUrl | None:
    upload_match = _UPLOAD_FILE_PATH_PATTERN.match(path)
    if upload_match:
        preview_kind: Literal["file-preview", "image-preview"]
        if upload_match.group("preview_kind") == "image-preview":
            preview_kind = "image-preview"
        else:
            preview_kind = "file-preview"

        return _SignedFileUrl(
            file_id=upload_match.group("file_id"),
            preview_kind=preview_kind,
            record_kind="upload",
        )

    tool_match = _TOOL_FILE_PATH_PATTERN.match(path)
    if tool_match:
        return _SignedFileUrl(
            file_id=tool_match.group("file_id"),
            preview_kind="file-preview",
            record_kind="tool",
        )

    datasource_match = _DATASOURCE_FILE_PATH_PATTERN.match(path)
    if datasource_match:
        return _SignedFileUrl(
            file_id=datasource_match.group("file_id"),
            preview_kind="file-preview",
            record_kind="datasource",
        )

    return None


def _is_dify_file_origin(parsed_url: urllib.parse.ParseResult) -> bool:
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return False

    url_origin = _origin_parts(parsed_url)
    if url_origin is None:
        return False

    allowed_origins = {
        origin
        for configured_url in [dify_config.FILES_URL, dify_config.INTERNAL_FILES_URL]
        if configured_url and (origin := _origin_parts(urllib.parse.urlparse(configured_url))) is not None
    }
    return url_origin in allowed_origins


def _origin_parts(parsed_url: urllib.parse.ParseResult) -> tuple[str, str, int] | None:
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return None
    try:
        port = parsed_url.port
    except ValueError:
        return None
    return parsed_url.scheme, parsed_url.hostname.lower(), port or _default_port(parsed_url.scheme)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _single_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values or len(values) != 1:
        return None
    return values[0]


def _verify_signed_file_url(
    *,
    signed_file_url: _SignedFileUrl,
    timestamp: str,
    nonce: str,
    sign: str,
) -> bool:
    try:
        current_time = int(time.time())
        signed_at = int(timestamp)
    except ValueError:
        return False

    if current_time - signed_at > dify_config.FILES_ACCESS_TIMEOUT:
        return False

    payload = f"{signed_file_url.preview_kind}|{signed_file_url.file_id}|{timestamp}|{nonce}"
    recalculated = hmac.new(dify_config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(recalculated).decode()
    return hmac.compare_digest(sign, expected)


def _build_upload_file_response(*, method: Literal["GET", "HEAD"], url: str, file_id: str) -> httpx.Response:
    with session_factory.create_session() as session:
        upload_file = _file_access_controller.get_upload_file(session=session, file_id=file_id)
    if upload_file is None:
        return _build_response(method=method, url=url, status_code=404)

    content = b"" if method == "HEAD" else storage.load_once(upload_file.key)
    return _build_response(
        method=method,
        url=url,
        status_code=200,
        content=content,
        content_length=upload_file.size,
        content_type=upload_file.mime_type,
        filename=upload_file.name,
    )


def _build_tool_file_response(*, method: Literal["GET", "HEAD"], url: str, file_id: str) -> httpx.Response:
    with session_factory.create_session() as session:
        tool_file = _file_access_controller.get_tool_file(session=session, file_id=file_id)
    if tool_file is None:
        return _build_response(method=method, url=url, status_code=404)

    content = b"" if method == "HEAD" else storage.load_once(tool_file.file_key)
    return _build_response(
        method=method,
        url=url,
        status_code=200,
        content=content,
        content_length=tool_file.size,
        content_type=tool_file.mimetype,
        filename=tool_file.name,
    )


def _build_datasource_file_response(*, method: Literal["GET", "HEAD"], url: str, file_id: str) -> httpx.Response:
    with session_factory.create_session() as session:
        upload_file = _file_access_controller.get_upload_file(session=session, file_id=file_id)
        if upload_file is not None:
            return _build_upload_file_record_response(method=method, url=url, upload_file=upload_file)

        tool_file = _file_access_controller.get_tool_file(session=session, file_id=file_id)
        if tool_file is not None:
            return _build_tool_file_record_response(method=method, url=url, tool_file=tool_file)

    return _build_response(method=method, url=url, status_code=404)


def _build_upload_file_record_response(
    *,
    method: Literal["GET", "HEAD"],
    url: str,
    upload_file: UploadFile,
) -> httpx.Response:
    content = b"" if method == "HEAD" else storage.load_once(upload_file.key)
    return _build_response(
        method=method,
        url=url,
        status_code=200,
        content=content,
        content_length=upload_file.size,
        content_type=upload_file.mime_type,
        filename=upload_file.name,
    )


def _build_tool_file_record_response(
    *,
    method: Literal["GET", "HEAD"],
    url: str,
    tool_file: ToolFile,
) -> httpx.Response:
    content = b"" if method == "HEAD" else storage.load_once(tool_file.file_key)
    return _build_response(
        method=method,
        url=url,
        status_code=200,
        content=content,
        content_length=tool_file.size,
        content_type=tool_file.mimetype,
        filename=tool_file.name,
    )


def _build_response(
    *,
    method: Literal["GET", "HEAD"],
    url: str,
    status_code: int,
    content: bytes = b"",
    content_length: int | None = None,
    content_type: str | None = None,
    filename: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if content_length is not None and content_length >= 0:
        headers["Content-Length"] = str(content_length)
    if filename:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=content,
        request=httpx.Request(method, url),
    )


graphon_remote_file_fetcher = GraphonRemoteFileFetcher()


_SENSITIVE_FILE_LIMIT = 2 * 1024 * 1024
_sensitive_file_request: ContextVar[bool] = ContextVar("sensitive_file_request", default=False)
_sensitive_filter_lock = threading.Lock()
_sensitive_filter_installed = False
_SENSITIVE_HTTP_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.proxy",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.socks",
)


class _SensitiveFileLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _sensitive_file_request.get()


def _install_sensitive_file_log_filter() -> None:
    """Permanent emitter filters; ordinary concurrent requests retain their logs."""
    global _sensitive_filter_installed
    with _sensitive_filter_lock:
        if not _sensitive_filter_installed:
            sensitive_filter = _SensitiveFileLogFilter()
            for name in _SENSITIVE_HTTP_LOGGERS:
                logging.getLogger(name).addFilter(sensitive_filter)
            _sensitive_filter_installed = True


@dataclass(frozen=True, slots=True)
class BoundedExternalFile:
    """Closed outcome, with no remote metadata or exception objects.

    ``confirmed`` describes local cleanup by the original deadline owner, never
    remote business termination. A cancelled or unknown outcome requires the
    caller to stop; it must not retry. Content is deliberately absent from repr.
    """

    status: Literal["ok", "rejected", "failed", "cancelled", "unknown"]
    reason: Literal[
        "ok",
        "invalid_arguments",
        "expired",
        "proxy_required",
        "privacy_unsupported",
        "runtime_unsupported",
        "invalid_url",
        "local_origin",
        "supplier_failed",
        "http_status",
        "encoded_response",
        "too_large",
        "proxy_denied",
        "request_failed",
        "cancelled",
        "termination_unconfirmed",
    ]
    termination: Literal["confirmed", "unconfirmed"] = "confirmed"
    content: bytes = field(default=b"", repr=False)


def _sensitive_file_url_allowed(value: object) -> bool:
    """Conservative HTTPS syntax matching avatar admission; no DNS assertion."""
    try:
        if (
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > 4096
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or "\\" in value
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            return False
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        decoded = urllib.parse.unquote(value, errors="strict")
        if (
            parsed.scheme != "https"
            or not host
            or "%" in host
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or parsed.port not in (None, 443)
            or len(parsed.path.encode("utf-8")) > 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or "\\" in decoded
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return (
                host.isascii()
                and len(host) <= 253
                and "." in host
                and not host.endswith((".localhost", ".local", ".internal"))
                and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x) for x in host.split("."))
                and not host.split(".")[-1].isdigit()
            )
        return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)
    except (ValueError, UnicodeError):
        return False


def _sensitive_file_transfer(
    url_supplier: Callable[[], str], expires_at: float
) -> tuple[BoundedExternalFile, Literal["interrupt", "exit"] | None]:
    """Must run entirely inside all private contexts, including exception catches."""
    url = None
    response = None
    supplied = False
    try:
        if time.monotonic() >= expires_at:
            return BoundedExternalFile("rejected", "expired"), None
        url = url_supplier()
        supplied = True
        if not _sensitive_file_url_allowed(url):
            return BoundedExternalFile("rejected", "invalid_url"), None
        if _is_dify_file_origin(urllib.parse.urlparse(url)):
            return BoundedExternalFile("rejected", "local_origin"), None
        # No signed-file resolution, caller options, cookies, authorization or
        # arbitrary Host. The original transport still owns trace propagation.
        response = ssrf_proxy.make_request_with_deadline(
            "GET",
            url,
            deadline=expires_at,
            request_timeout=15.0,
            max_response_bytes=_SENSITIVE_FILE_LIMIT,
            headers={"Accept-Encoding": "identity"},
        )
        if response.status_code != 200:
            return BoundedExternalFile("rejected", "http_status"), None
        return BoundedExternalFile("ok", "ok", content=response.content), None
    except ssrf_proxy.RequestTerminationUnconfirmedError:
        return BoundedExternalFile("unknown", "termination_unconfirmed", "unconfirmed"), None
    except ssrf_proxy.RequestDeadlineExceededError:
        return BoundedExternalFile("failed", "expired"), None
    except ssrf_proxy.ResponseTooLargeError:
        return BoundedExternalFile("rejected", "too_large"), None
    except ssrf_proxy.UnsupportedResponseEncodingError:
        return BoundedExternalFile("rejected", "encoded_response"), None
    except ssrf_proxy.ToolSSRFError:
        return BoundedExternalFile("rejected", "proxy_denied"), None
    except asyncio.CancelledError:
        # The original async owner finishes cleanup before propagating this;
        # cleanup failure instead raises RequestTerminationUnconfirmedError.
        return BoundedExternalFile("cancelled", "cancelled"), None
    except KeyboardInterrupt:
        return BoundedExternalFile("unknown", "termination_unconfirmed", "unconfirmed"), "interrupt"
    except SystemExit:
        return BoundedExternalFile("unknown", "termination_unconfirmed", "unconfirmed"), "exit"
    except Exception:
        return BoundedExternalFile("failed", "request_failed" if supplied else "supplier_failed"), None
    finally:
        # Best-effort reference reduction before restoring inherited SDK scopes;
        # this is neither memory erasure nor proof of physical cancellation.
        url = response = url_supplier = None


def fetch_bounded_external_file(url_supplier: Callable[[], str], expires_at: float) -> BoundedExternalFile:
    """Fetch one sensitive external file through the original bounded SSRF owner.

    ``expires_at`` is a finite absolute monotonic deadline. The supplier must
    decrypt synchronously without external I/O; its work cannot be preempted.
    Arguments, supported runtime, configured proxy and local privacy contexts
    are checked before invoking it. The HTTP/cleanup budget is at most 15s and
    also bounded by that deadline; the original stream enforces 2MiB before append.

    Normal errors and cancellation return only fixed closed outcomes. Stop on
    cancelled/unknown. Interpreter shutdown and keyboard interruption propagate
    as NEW sanitized signals outside the original exception and private scopes;
    their original args/code/traceback are intentionally not exported. This does
    not prove proxy-side log privacy, DNS safety or physical socket termination
    beyond what the original deadline owner actually confirms.
    """
    if not callable(url_supplier) or type(expires_at) not in (float, int):
        return BoundedExternalFile("rejected", "invalid_arguments")
    try:
        if not math.isfinite(expires_at):
            return BoundedExternalFile("rejected", "invalid_arguments")
    except OverflowError:
        return BoundedExternalFile("rejected", "invalid_arguments")
    if expires_at <= time.monotonic():
        return BoundedExternalFile("rejected", "expired")
    if not (dify_config.SSRF_PROXY_ALL_URL or (dify_config.SSRF_PROXY_HTTP_URL and dify_config.SSRF_PROXY_HTTPS_URL)):
        return BoundedExternalFile("rejected", "proxy_required")
    if ssrf_proxy._DEADLINE_RUNTIME_VERSIONS != ("0.28.1", "1.0.9", "4.14.1"):
        return BoundedExternalFile("rejected", "runtime_unsupported")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return BoundedExternalFile("rejected", "runtime_unsupported")

    outcome = BoundedExternalFile("rejected", "privacy_unsupported")
    signal = None
    token = _sensitive_file_request.set(True)
    try:
        # Pin the inspected APIs. Missing/incompatible SDKs fail before decrypt,
        # without changing the behavior of the legacy remote-file API.
        if version("sentry-sdk") != "2.57.0" or version("opentelemetry-instrumentation") != "0.65b0":
            return outcome
        import sentry_sdk
        from opentelemetry.instrumentation.utils import suppress_http_instrumentation
        from sentry_sdk.client import BaseClient
        from sentry_sdk.scope import use_isolation_scope, use_scope

        class PrivateClient(BaseClient):
            def is_active(self) -> bool:
                # Avoid Scope.get_client's fallback to a recording global client.
                # All capture/get_integration behavior is inherited no-op.
                return True

        private_client = PrivateClient()
        _install_sensitive_file_log_filter()
        with ExitStack() as contexts:
            contexts.enter_context(suppress_http_instrumentation())
            # Scope(client=...) / set_client() also writes global SDK attributes
            # in this installed SDK. Bind only the NEW scopes' public client slot.
            isolation = sentry_sdk.Scope()
            current = sentry_sdk.Scope()
            isolation.client = current.client = private_client
            contexts.enter_context(use_isolation_scope(isolation))
            contexts.enter_context(use_scope(current))
            if sentry_sdk.get_client() is not private_client or current.span is not None or isolation.span is not None:
                return outcome
            outcome, signal = _sensitive_file_transfer(url_supplier, expires_at)
            url_supplier = None
            # A supplier may add private breadcrumbs/extras to these fresh
            # scopes. Do not retain those scopes in a later shutdown traceback.
            current = isolation = None
    except Exception:
        # Context setup failure happens before decryption. A context teardown
        # failure after transfer must not turn uncertain privacy into success.
        outcome = BoundedExternalFile("unknown", "privacy_unsupported", "unconfirmed")
    finally:
        url_supplier = None
        _sensitive_file_request.reset(token)
    # These raises are outside every except block: from None alone would still
    # leave the original sensitive exception in __context__.
    if signal == "interrupt":
        raise KeyboardInterrupt("sensitive file request interrupted")
    if signal == "exit":
        raise SystemExit(1)
    return outcome
