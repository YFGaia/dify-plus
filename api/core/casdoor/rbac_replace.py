"""Single-attempt RBAC replace syntax in an explicit synthetic offline domain.

Actual release/full-visibility/sync-principal evidence and fresh managed DB
scope are missing. Production therefore rejects before configuration or HTTP.
Raw fixture consistency, a parsed PUT response and client cleanup are never
authorization, remote quiescence, APPLIED/CONFIRMED or finalization proof.
HTTP must remain outside the future caller's write UoW; this module owns no DB.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from models.account import TenantAccountRole

from core.casdoor import rbac_read as raw
from core.casdoor import rbac_reader as reader
from core.casdoor.configuration import TargetRole
from core.casdoor.ownership import has_remote_owner
from core.helper import ssrf_proxy


class RbacReplaceError(raw.RbacCandidateError):
    """Fixed pending error with business uncertainty separate from client cleanup."""

    def __init__(self, *, possibly_submitted: bool = False, client_termination_confirmed: bool = True) -> None:
        super().__init__()
        self.possibly_submitted = possibly_submitted
        self.business_unknown = possibly_submitted
        self.client_termination_confirmed = client_termination_confirmed


def replace_rbac(*, capability: object = None, **operation: object) -> None:
    """Unconditionally closed production gate; no runtime capability is issued.

    Even a private offline receipt, public candidate, bool or hostile lazy object
    cannot enable a deployment mutation. C2/I14-B must establish actual runtime
    attempt/managed scope provenance after B3/G0 establishes the read contract.
    """
    raise RbacReplaceError()


@dataclass(frozen=True, repr=False)
class OfflineRbacAcknowledgment:
    """Synthetic matching response candidates, never business completion proof."""

    desired: raw.DesiredBuiltinCandidate
    member: raw.MemberRolesCandidate


@dataclass(frozen=True, repr=False)
class _OfflineReplaceReceipt:
    _nonce: object
    _result: OfflineRbacAcknowledgment


@dataclass(repr=False)
class _OfflineReplaceOperation:
    """Explicit injected offline transport only; no default HTTP or DB owner.

    B2's private read operation is reused solely as its context/lease/deadline
    guard. It never performs a GET here. The absolute deadline includes earlier
    caller reads and remains unchanged. Existing synchronous lease checks can
    reject late results but cannot physically interrupt a blocked Redis call.

    A local NORMAL enum and raw prior roles/catalog prove fixture consistency
    only, not fresh authenticated ownership or authority. Once the transport
    can have been invoked, every failure is sticky business UNKNOWN, regardless
    of client cleanup. This operation cannot retry, compensate or read back.
    """

    context: reader._OfflineReadContext
    leases: reader._LeaseGuard = field(repr=False)
    transport: Callable[..., httpx.Response] = field(repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _nonce: object = field(default_factory=object, init=False, repr=False)
    _guard: reader._OfflineReadOperation = field(init=False, repr=False)
    _bound_transport: Callable[..., httpx.Response] = field(init=False, repr=False)
    _attempted: bool = field(default=False, init=False)
    _possibly_submitted: bool = field(default=False, init=False)
    _halted: bool = field(default=False, init=False)
    _client_termination_confirmed: bool = field(default=True, init=False)
    _issued: _OfflineReplaceReceipt | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            self._guard = reader._OfflineReadOperation(self.context, self.leases, self.transport, self.monotonic)
        except BaseException:
            self._fail()
        self._bound_transport = self.transport

    def _fail(self, *, client_termination_confirmed: bool = True) -> Any:
        self._halted = True
        self._issued = None
        self._client_termination_confirmed = self._client_termination_confirmed and client_termination_confirmed
        raise RbacReplaceError(
            possibly_submitted=self._possibly_submitted,
            client_termination_confirmed=self._client_termination_confirmed,
        ) from None

    def _check(self) -> None:
        try:
            if (
                self._halted
                or self.context is not self._guard.context
                or self.leases is not self._guard.leases
                or self.transport is not self._bound_transport
                or self.monotonic is not self._guard.monotonic
            ):
                self._fail()
            self._guard._check()
        except BaseException:
            self._fail()

    def replace(
        self,
        *,
        prior_member: bytes,
        catalog_pages: tuple[bytes, ...],
        target_role: TargetRole,
        local_join_role: TenantAccountRole,
    ) -> _OfflineReplaceReceipt:
        """Resolve one exact builtin singleton before one exact scoped PUT.

        Empty targets/removal belong I17. Raw parsing reuses B1's duplicate-key,
        type, scope, field and byte contracts rather than Enterprise DTO defaults.
        No public candidate/dataclass or arbitrary role IDs are admitted.
        """
        self._check()
        if self._attempted:
            self._fail()
        self._attempted = True
        ctx = self.context
        try:
            if local_join_role is not TenantAccountRole.NORMAL:
                self._fail()
            prior = raw.decode_member_candidate(
                prior_member,
                workspace_id=ctx.workspace_id,
                account_id=ctx.account_id,
                contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1,
            )
            if has_remote_owner(prior.roles):
                self._fail()
            self._check()
            catalog = raw.decode_catalog_candidate(
                catalog_pages, workspace_id=ctx.workspace_id, contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1
            )
            desired = raw.desired_builtin_candidate(catalog, target_role)
            self._check()
            # Conservative latch before entering the injected transport: even
            # an exception with confirmed local cleanup cannot prove no PUT.
            self._possibly_submitted = True
            response = self.transport(
                "PUT",
                ctx.base_url + "/rbac/members/rbac-roles",
                deadline=ctx.deadline,
                max_response_bytes=raw.MAX_MEMBER_BYTES,
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
                params={"account_id": str(ctx.account_id)},
                json={"role_ids": [desired.roles[0].role_id]},
            )
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
            member = raw.decode_member_candidate(
                response.content,
                workspace_id=ctx.workspace_id,
                account_id=ctx.account_id,
                contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1,
            )
            if member.roles != desired.roles:
                self._fail()
            self._check()
            receipt = _OfflineReplaceReceipt(self._nonce, OfflineRbacAcknowledgment(desired, member))
            self._issued = receipt
            return receipt
        except ssrf_proxy.RequestTerminationUnconfirmedError:
            self._fail(client_termination_confirmed=False)
        except BaseException:
            # Cancellation/interrupts cannot bypass the uncertainty latch. The
            # offline harness maps them to the same safe, stopped domain error.
            self._fail()

    def extract(self, receipt: object) -> OfflineRbacAcknowledgment:
        self._check()
        if (
            type(receipt) is not _OfflineReplaceReceipt
            or receipt is not self._issued
            or receipt._nonce is not self._nonce
        ):
            self._fail()
        self._issued = None
        return receipt._result
