"""Bounded RBAC read engine and private offline issuing boundary.

No deployment release/full-visibility/sync-principal evidence exists yet. The
production entry point therefore rejects every capability before configuration
or network access. The explicit offline domain exercises transport syntax and
candidate completeness only; it cannot issue authenticated COMPLETE projections
or mapping BuiltinResolution values. HTTP belongs outside every DB write UoW;
this module accepts no Session and performs no SQL, writes or remote mutation.
"""

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from core.casdoor import rbac_read as raw
from core.casdoor.configuration import TargetRole
from core.helper import ssrf_proxy


class RbacReadError(raw.RbacCandidateError):
    """Fixed pending error; no credentials, response text or role data."""

    def __init__(self, *, termination_confirmed: bool = True) -> None:
        super().__init__()
        self.termination_confirmed = termination_confirmed


def read_rbac(*, capability: object = None, **operation: object) -> None:
    """Unavailable production capability; no runtime issuer/registry is enabled.

    None, booleans, public candidates, offline receipts and arbitrary objects all
    reject before reading configuration or invoking transport. A future G0
    packet must establish a supported contract and server-owned issuing owner.
    """
    raise RbacReadError()


class _LeaseGuard(Protocol):
    """Existing CasdoorLeases shape; the offline harness supplies a fake owner."""

    _deadline: float

    @property
    def canonical_keys(self) -> tuple[str, ...]: ...

    def ensure_owned(self, *, renew: bool = False) -> None: ...


@dataclass(frozen=True, repr=False)
class _OfflineReadContext:
    """Explicit synthetic scope, never actual principal/full visibility proof.

    The executing account is separate from the member being inspected. Never
    derive it from Flask/current user or impersonate an Owner. Options reproduce
    current enterprise syntax, including the existing biiling_enabled spelling.
    The context and credentials are omitted from repr and safe errors.
    """

    workspace_id: UUID
    account_id: UUID
    principal_id: UUID
    base_url: str
    secret: str
    deadline: float
    billing_enabled: bool = False
    dataset_operator_enabled: bool = False


@dataclass(frozen=True, repr=False)
class OfflineRbacRead:
    """Synthetic candidates only, with no knowledge/provenance grant fields."""

    member: raw.MemberRolesCandidate
    catalog: raw.RoleCatalogCandidate
    desired: tuple[raw.DesiredBuiltinCandidate, ...]


@dataclass(frozen=True, repr=False)
class _OfflineReceipt:
    _nonce: object
    _result: OfflineRbacRead


@dataclass(repr=False)
class _OfflineReadOperation:
    """Single-use fixture operation, explicit transport seam required.

    Passing the shared SSRF owner is permitted only in offline tests that patch
    it before invocation. There is no default transport or production issuance
    path. A receipt's identity and operation nonce bind exact immutable context;
    extracting it rechecks leases/deadline and consumes it. These checks prove
    only the offline domain, never real backend authorization or PUT termination.

    Lease checks use existing synchronous Redis ownership/TTL checks. They can
    reject late results but cannot physically preempt a blocked Redis call.
    """

    context: _OfflineReadContext
    leases: _LeaseGuard = field(repr=False)
    transport: Callable[..., httpx.Response] = field(repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _nonce: object = field(default_factory=object, init=False, repr=False)
    _started: bool = field(default=False, init=False)
    _halted: bool = field(default=False, init=False)
    _termination_confirmed: bool = field(default=True, init=False)
    _issued: _OfflineReceipt | None = field(default=None, init=False, repr=False)
    _bound_context: _OfflineReadContext | None = field(default=None, init=False, repr=False)
    _bound_leases: _LeaseGuard | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        ctx = self.context
        try:
            parsed = urlsplit(ctx.base_url)
            valid_url = (
                type(ctx.base_url) is str
                and len(ctx.base_url.encode("utf-8")) <= 2048
                and parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not ctx.base_url.endswith("/")
                and not any(ord(char) <= 32 or ord(char) == 127 for char in ctx.base_url)
            )
            now = self.monotonic()
            valid = (
                type(ctx) is _OfflineReadContext
                and all(type(value) is UUID for value in (ctx.workspace_id, ctx.account_id, ctx.principal_id))
                and valid_url
                and type(ctx.secret) is str
                and 0 < len(ctx.secret) <= 16 * 1024
                and ctx.secret.isascii()
                and not any(ord(char) < 32 or ord(char) == 127 for char in ctx.secret)
                and type(ctx.deadline) in (int, float)
                and math.isfinite(ctx.deadline)
                and now < ctx.deadline <= now + 45.0
                and type(ctx.billing_enabled) is bool
                and type(ctx.dataset_operator_enabled) is bool
                and self.leases._deadline == ctx.deadline
                and f"casdoor:lease:v1:account:{ctx.account_id}" in self.leases.canonical_keys
                and f"casdoor:lease:v1:member:{ctx.workspace_id}:{ctx.account_id}" in self.leases.canonical_keys
                and callable(self.transport)
            )
        except Exception:
            self._fail()
        if not valid:
            self._fail()
        self._bound_context = ctx
        self._bound_leases = self.leases
        self._check()

    def _fail(self, *, termination_confirmed: bool = True) -> Any:
        self._halted = True
        self._issued = None
        self._termination_confirmed = self._termination_confirmed and termination_confirmed
        raise RbacReadError(termination_confirmed=self._termination_confirmed) from None

    def _check(self) -> None:
        if (
            self._halted
            or self.context is not self._bound_context
            or self.leases is not self._bound_leases
            or self.monotonic() >= self.context.deadline
        ):
            self._fail()
        try:
            if (
                self.leases._deadline != self.context.deadline
                or f"casdoor:lease:v1:account:{self.context.account_id}" not in self.leases.canonical_keys
                or f"casdoor:lease:v1:member:{self.context.workspace_id}:{self.context.account_id}"
                not in self.leases.canonical_keys
            ):
                self._fail()
            self.leases.ensure_owned()
        except Exception:
            self._fail()
        if self.monotonic() >= self.context.deadline:
            self._fail()

    def _request(self, path: str, params: dict[str, object], limit: int) -> bytes:
        self._check()
        ctx = self.context
        member_request = (
            path == "/rbac/members/rbac-roles"
            and params == {"account_id": str(ctx.account_id)}
            and limit == raw.MAX_MEMBER_BYTES
        )
        catalog_request = (
            path == "/rbac/roles"
            and set(params)
            == {"page_number", "results_per_page", "include_owner", "biiling_enabled", "dataset_operator_enabled"}
            and type(params["page_number"]) is int
            and 1 <= params["page_number"] <= raw.MAX_CATALOG_PAGES
            and type(params["results_per_page"]) is int
            and params["results_per_page"] == raw.MAX_PER_PAGE
            and type(params["include_owner"]) is int
            and params["include_owner"] == 1
            and params["biiling_enabled"] is ctx.billing_enabled
            and params["dataset_operator_enabled"] is ctx.dataset_operator_enabled
            and type(limit) is int
            and 0 < limit <= raw.MAX_PAGE_BYTES
        )
        if not self._started or not (member_request or catalog_request):
            self._fail()
        try:
            response = self.transport(
                "GET",
                ctx.base_url + path,
                deadline=ctx.deadline,
                max_response_bytes=limit,
                request_timeout=15.0,
                max_retries=0,
                follow_redirects=False,
                ssl_verify=True,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Enterprise-Api-Secret-Key": ctx.secret,
                    "X-Inner-Tenant-Id": str(ctx.workspace_id),
                    "X-Inner-Account-Id": str(ctx.principal_id),
                },
                params=params,
            )
        except ssrf_proxy.RequestTerminationUnconfirmedError:
            self._fail(termination_confirmed=False)
        except Exception:
            self._fail()
        self._check()
        if (
            type(response) is not httpx.Response
            or response.status_code != 200
            or response.headers.get("content-encoding", "").strip().lower() not in ("", "identity")
            or not re.fullmatch(
                r'application/json(?:\s*;\s*charset\s*=\s*"?utf-8"?)?',
                response.headers.get("content-type", "").strip().lower(),
            )
        ):
            self._fail()
        try:
            body = response.content
            raw._check_bytes(body, limit)
        except Exception:
            self._fail()
        self._check()
        return body

    def read(self, target_roles: tuple[TargetRole, ...] = ()) -> _OfflineReceipt:
        self._check()
        if self._started or type(target_roles) is not tuple or len(target_roles) > 3:
            self._fail()
        self._started = True
        if any(type(role) is not str or role not in ("admin", "editor", "normal") for role in target_roles) or len(
            set(target_roles)
        ) != len(target_roles):
            self._fail()
        ctx = self.context
        try:
            member = raw.decode_member_candidate(
                self._request("/rbac/members/rbac-roles", {"account_id": str(ctx.account_id)}, raw.MAX_MEMBER_BYTES),
                workspace_id=ctx.workspace_id,
                account_id=ctx.account_id,
                contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1,
            )
            self._check()
            pages: list[bytes] = []
            total_bytes = 0
            expected: tuple[int, int] | None = None
            collected_roles = ()
            page_number = 1
            while True:
                self._check()
                if page_number > raw.MAX_CATALOG_PAGES or total_bytes >= raw.MAX_CATALOG_BYTES:
                    self._fail()
                body = self._request(
                    "/rbac/roles",
                    {
                        "page_number": page_number,
                        "results_per_page": raw.MAX_PER_PAGE,
                        "include_owner": 1,
                        "biiling_enabled": ctx.billing_enabled,
                        "dataset_operator_enabled": ctx.dataset_operator_enabled,
                    },
                    min(raw.MAX_PAGE_BYTES, raw.MAX_CATALOG_BYTES - total_bytes),
                )
                total_bytes += len(body)
                if total_bytes > raw.MAX_CATALOG_BYTES:
                    self._fail()
                # Reuse B1 strict JSON, fields, exact integers and canonical role
                # validation before allowing a subsequent page dispatch.
                envelope = raw._fields(raw._decode(body), {"data", "pagination"})
                meta = raw._fields(envelope["pagination"], {"total_count", "per_page", "current_page", "total_pages"})
                total = raw._integer(meta["total_count"], minimum=0, maximum=raw.MAX_MEMBER_ROLES)
                per_page = raw._integer(meta["per_page"], minimum=1, maximum=raw.MAX_PER_PAGE)
                current = raw._integer(meta["current_page"], minimum=1, maximum=raw.MAX_CATALOG_PAGES)
                count = raw._integer(meta["total_pages"], minimum=1, maximum=raw.MAX_CATALOG_PAGES)
                signature = (total, count)
                page_roles = raw._roles(envelope["data"], ctx.workspace_id, limit=per_page)
                if (
                    per_page != raw.MAX_PER_PAGE
                    or current != page_number
                    or count != max(1, (total + per_page - 1) // per_page)
                    or (expected is not None and signature != expected)
                    or len(page_roles) != min(per_page, max(0, total - (page_number - 1) * per_page))
                ):
                    self._fail()
                expected = signature
                collected_roles = raw._canonical(collected_roles + page_roles)
                pages.append(body)
                self._check()
                if page_number == count:
                    break
                page_number += 1
            catalog = raw.decode_catalog_candidate(
                tuple(pages), workspace_id=ctx.workspace_id, contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1
            )
            desired = tuple(raw.desired_builtin_candidate(catalog, target) for target in target_roles)
            self._check()
            receipt = _OfflineReceipt(self._nonce, OfflineRbacRead(member, catalog, desired))
            self._issued = receipt
            return receipt
        except RbacReadError:
            raise
        except Exception:
            self._fail()

    def extract(self, receipt: object) -> OfflineRbacRead:
        self._check()
        if type(receipt) is not _OfflineReceipt or receipt is not self._issued or receipt._nonce is not self._nonce:
            self._fail()
        self._issued = None
        return receipt._result
