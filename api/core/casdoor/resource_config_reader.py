"""Bounded synthetic resource observations; production issuance stays disabled.

Typed public inventory fields establish consistency, never database provenance.
The caller must reconstruct ownership and finish its local read transaction
before HTTP. No Session, grant, ACL baseline, queue or session proof is accepted
or issued. SSRF cleanup concerns the local HTTP read only. Synchronous lease
ownership I/O has logical pre/post checks, not physical Redis cancellation (F06).
"""

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from core.casdoor import resource_config_read as raw
from core.casdoor.rbac_reader import _LeaseGuard
from core.helper import ssrf_proxy
from repositories.casdoor_local_resource_repository_extend import LocalResourceInventory

MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 9


class ResourceConfigReadError(ValueError):
    def __init__(self, *, termination_confirmed: bool = True) -> None:
        super().__init__("resource_config_read_pending")
        self.termination_confirmed = termination_confirmed


def read_resource_config(*, capability: object = None, **operation: object) -> None:
    """Unconditional rejection before configuration, credential or transport access."""
    raise ResourceConfigReadError()


@dataclass(frozen=True, repr=False)
class _OfflineReadContext:
    workspace_id: UUID
    account_id: UUID
    principal_id: UUID
    base_url: str
    secret: str
    deadline: float
    contract: raw.ResourceConfigContract = raw.ResourceConfigContract.OFFLINE_FIXTURE_V1


@dataclass(frozen=True, repr=False)
class OfflineResourceConfigRead:
    """Only synthetic observations; even an empty result conveys no remote knowledge."""

    batches: tuple[raw.ResourceConfigReadCandidate, ...]


@dataclass(frozen=True, repr=False)
class _OfflineReceipt:
    _nonce: object
    _result: OfflineResourceConfigRead


@dataclass(repr=False)
class _OfflineReadOperation:
    """Single-use explicit fake transport seam, never a server authority issuer.

    The actual shared SSRF symbol may only be injected when patched in offline
    tests. No default transport, retry, pagination or background cleanup exists.
    Object identity guards belong to this fixture domain, not authentication.
    """

    context: _OfflineReadContext
    leases: _LeaseGuard
    transport: Callable[..., httpx.Response]
    monotonic: Callable[[], float] = time.monotonic
    _nonce: object = field(default_factory=object, init=False)
    _context_id: UUID = field(default_factory=uuid4, init=False)
    _started: bool = field(default=False, init=False)
    _halted: bool = field(default=False, init=False)
    _termination_confirmed: bool = field(default=True, init=False)
    _issued: _OfflineReceipt | None = field(default=None, init=False)
    _bound: tuple = field(default=(), init=False)
    _keys: tuple[str, ...] = field(default=(), init=False)

    def _fail(self, *, termination_confirmed: bool = True) -> Any:
        self._halted = True
        self._issued = None
        self._termination_confirmed &= termination_confirmed
        raise ResourceConfigReadError(termination_confirmed=self._termination_confirmed) from None

    def __post_init__(self) -> None:
        try:
            ctx = self.context
            parsed = urlsplit(ctx.base_url)
            now = self.monotonic()
            if not (
                type(ctx) is _OfflineReadContext
                and all(type(v) is UUID for v in (ctx.workspace_id, ctx.account_id, ctx.principal_id))
                and ctx.contract is raw.ResourceConfigContract.OFFLINE_FIXTURE_V1
                and type(ctx.base_url) is str
                and len(ctx.base_url.encode()) <= 2048
                and parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not ctx.base_url.endswith("/")
                and not any(ord(c) <= 32 or ord(c) == 127 for c in ctx.base_url)
                and type(ctx.secret) is str
                and 0 < len(ctx.secret) <= 16 * 1024
                and ctx.secret.isascii()
                and not any(ord(c) < 32 or ord(c) == 127 for c in ctx.secret)
                and type(ctx.deadline) in (int, float)
                and math.isfinite(ctx.deadline)
                and type(now) in (int, float)
                and math.isfinite(now)
                and now < ctx.deadline <= now + 45
                and callable(self.transport)
            ):
                self._fail()
            self._keys = self.leases.canonical_keys
            self._bound = (ctx, self.leases, self.transport, self.monotonic)
            self._check()
        except Exception:
            self._fail()

    def _check(self) -> None:
        try:
            ctx = self.context
            now = self.monotonic()
            if (
                self._halted
                or any(a is not b for a, b in zip(self._bound, (ctx, self.leases, self.transport, self.monotonic)))
                or not math.isfinite(now)
                or now >= ctx.deadline
                or type(self.leases._deadline) not in (int, float)
                or self.leases._deadline != ctx.deadline
                or type(self.leases.canonical_keys) is not tuple
                or self.leases.canonical_keys != self._keys
                or f"casdoor:lease:v1:account:{ctx.account_id}" not in self._keys
                or f"casdoor:lease:v1:member:{ctx.workspace_id}:{ctx.account_id}" not in self._keys
            ):
                self._fail()
            self.leases.ensure_owned()
            now = self.monotonic()
            if (
                not math.isfinite(now)
                or now >= ctx.deadline
                or any(
                    a is not b for a, b in zip(self._bound, (self.context, self.leases, self.transport, self.monotonic))
                )
                or self.leases._deadline != ctx.deadline
                or self.leases.canonical_keys != self._keys
            ):
                self._fail()
        except Exception:
            self._fail()
        except BaseException:
            self._fail(termination_confirmed=False)

    def _request(self, request: raw.ResourceConfigRequestCandidate, limit: int) -> bytes:
        self._check()
        try:
            ctx = self.context
            response = self.transport(
                "POST",
                ctx.base_url + "/rbac/whitelist/configs",
                content=request.body,
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
            )
        except ssrf_proxy.RequestTerminationUnconfirmedError:
            self._fail(termination_confirmed=False)
        except Exception:
            self._fail()
        except BaseException:
            # Cancellation cannot leave a reusable operation or late receipt.
            self._fail(termination_confirmed=False)
        self._check()
        try:
            if (
                type(response) is not httpx.Response
                or response.status_code != 200
                or response.headers.get("content-encoding", "").strip().lower() not in ("", "identity")
                or not re.fullmatch(
                    r'application/json(?:\s*;\s*charset\s*=\s*"?utf-8"?)?',
                    response.headers.get("content-type", "").strip().lower(),
                )
                or type(response.content) is not bytes
                or not 0 < len(response.content) <= limit
            ):
                self._fail()
            return response.content
        except Exception:
            self._fail()

    def read(self, inventory: LocalResourceInventory, selected: tuple[raw.ResourcePair, ...]) -> _OfflineReceipt:
        self._check()
        if self._started:
            self._fail()
        self._started = True
        try:
            if (
                type(inventory) is not LocalResourceInventory
                or type(inventory.workspace_id) is not UUID
                or inventory.workspace_id != self.context.workspace_id
                or type(inventory.status) is not str
                or inventory.status != "LOCAL_SCAN_EXHAUSTED"
                or type(inventory.attempted_queries) is not int
                or not 3 <= inventory.attempted_queries <= 16
            ):
                self._fail()
            raw._validate_pairs(inventory.resources, raw.MAX_INVENTORY)
            raw._validate_pairs(selected, raw.MAX_INVENTORY)
            # This validates exact original-enum inventory order even for empty selection.
            raw.build_resource_config_request(
                contract=self.context.contract,
                workspace_id=inventory.workspace_id,
                context_id=self._context_id,
                inventory=inventory.resources,
                selected=(),
            )
            if not set(selected) <= set(inventory.resources):
                self._fail()
            minimum_queries = 3 + sum(
                (sum(pair[0] is kind for pair in inventory.resources) + raw.MAX_BATCH - 1) // raw.MAX_BATCH
                for kind in raw._KIND_ORDER
            )
            if inventory.attempted_queries < minimum_queries:
                self._fail()
            selected_set = set(selected)
            ordered = tuple(pair for pair in inventory.resources if pair in selected_set)
            batches = []
            total_bytes = total_accounts = total_items = 0
            for offset in range(0, len(ordered), raw.MAX_BATCH):
                self._check()
                if len(batches) >= MAX_REQUESTS or total_bytes >= MAX_TOTAL_BYTES:
                    self._fail()
                request = raw.build_resource_config_request(
                    contract=self.context.contract,
                    workspace_id=inventory.workspace_id,
                    context_id=self._context_id,
                    inventory=inventory.resources,
                    selected=ordered[offset : offset + raw.MAX_BATCH],
                )
                body = self._request(request, min(raw.MAX_RESPONSE_BYTES, MAX_TOTAL_BYTES - total_bytes))
                total_bytes += len(body)
                self._check()
                candidate = raw.parse_resource_config(body, request=request)
                total_accounts += candidate.validated_account_count
                total_items += sum(
                    state is not raw.ResourceConfigObservation.OMITTED_UNKNOWN for _, state in candidate.observations
                )
                if total_accounts > raw.MAX_ACCOUNTS or total_items > raw.MAX_INVENTORY:
                    self._fail()
                self._check()
                batches.append(candidate)
            self._check()
            receipt = _OfflineReceipt(self._nonce, OfflineResourceConfigRead(tuple(batches)))
            self._issued = receipt
            return receipt
        except ResourceConfigReadError:
            raise
        except Exception:
            self._fail()
        except BaseException:
            self._fail(termination_confirmed=False)

    def extract(self, receipt: object) -> OfflineResourceConfigRead:
        self._check()
        if type(receipt) is not _OfflineReceipt or receipt is not self._issued or receipt._nonce is not self._nonce:
            self._fail()
        self._issued = None
        return receipt._result
