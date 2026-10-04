"""Framework-neutral data contracts for account invitation activation."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InvitationLookup:
    workspace_id: str | None
    email: str | None
    token: str


@dataclass(frozen=True, slots=True)
class InvitationToken:
    account_id: str
    email: str
    workspace_id: str
    role: str | None = None
    requires_setup: bool | None = None


@dataclass(frozen=True, slots=True)
class AccountInvitation:
    account_id: str
    account_email: str
    account_status: str
    workspace_id: str
    workspace_name: str | None
    role: str | None
    requires_setup: bool | None


@dataclass(frozen=True, slots=True)
class AccountSetup:
    name: str
    interface_language: str
    timezone: str


@dataclass(frozen=True, slots=True)
class InvitedAccountObservation:
    """Closed-read local snapshot; not invitation finalization or authorization."""

    invitation: AccountInvitation
    initialized_at: datetime | None
    name: str
    interface_language: str | None
    timezone: str | None
    interface_theme: str | None


@dataclass(frozen=True, slots=True)
class ActivationCommand:
    invitation: InvitationLookup
    name: str | None = None
    interface_language: str | None = None
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class ActivationCheckData:
    workspace_name: str | None
    workspace_id: str
    email: str
    account_status: str
    requires_setup: bool


@dataclass(frozen=True, slots=True)
class ActivationCheckResult:
    is_valid: bool
    data: ActivationCheckData | None = None


@dataclass(frozen=True, slots=True)
class ActivationPersistenceResult:
    membership_created: bool


@dataclass(frozen=True, slots=True)
class InvitationAuthority:
    """Issuance facts only; never proof of current membership authority."""

    schema_version: int
    issuance_id: str
    lifecycle_id: str
    lifecycle_epoch: int
    token_digest: str
    join_id_at_issue: str | None


@dataclass(frozen=True, slots=True)
class VersionedInvitationPayload:
    account_id: str
    email: str = field(repr=False)
    workspace_id: str
    role: str
    requires_setup: bool
    invitation_authority: InvitationAuthority


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class VersionedInvitationObservation:
    """Same-store, same-process handle; Redis facts do not authorize login."""

    payload: VersionedInvitationPayload
    ttl_ms: int
    payload_digest: str
    key_digest: str
    _raw: bytes = field(repr=False)
    _physical_key: bytes = field(repr=False)
    _prefix: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InvitationObservationResult:
    status: Literal[
        "observed", "absent_or_expired", "capability_stale", "unsupported_version", "invalid", "unavailable", "unknown"
    ]
    observation: VersionedInvitationObservation | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class InvitationConsumptionResult:
    status: Literal["consumed", "replayed", "mismatch", "receipt_conflict", "unknown", "unavailable"]


@dataclass(frozen=True, slots=True)
class InvitationConsumptionReadback:
    status: Literal["confirmed", "not_confirmed", "unknown", "unavailable"]


@runtime_checkable
class VersionedInvitationTokenStore(Protocol):
    """Optional stronger capability, independent of legacy find/revoke."""

    def observe_versioned_invitation(self, token: str) -> InvitationObservationResult: ...

    def consume_versioned_invitation(
        self, observation: VersionedInvitationObservation, *, operation_id: str, receipt_ttl_seconds: int
    ) -> InvitationConsumptionResult: ...

    def read_invitation_consumption(
        self, observation: VersionedInvitationObservation, *, operation_id: str
    ) -> InvitationConsumptionReadback: ...


@dataclass(frozen=True, slots=True, repr=False)
class InvitationRecoveryBinding:
    """Caller-verified attempt scope; this DTO does not verify provider claims.

    The trusted caller must first match its independently verified namespace and
    subject to the committed operation and select the exact candidate account.
    """

    namespace_id: str
    identity_id: str
    account_id: str
    workspace_id: str


@dataclass(frozen=True, slots=True, repr=False)
class InvitationConsumptionRecovery:
    """Server-owned immutable recovery facts, never a consumption capability.

    Construct only from the exact P2 issuance and an already committed durable
    operation. The operation ID and expected receipt are never request fields.
    No bearer is retained here. The deadline is local to this authenticated
    request, not a durable clock or a receipt TTL extension.
    """

    namespace_id: str
    identity_id: str
    account_id: str
    workspace_id: str
    payload_json: str
    payload_digest: str
    operation_id: str
    expected_receipt: bytes
    deadline_monotonic: float


@runtime_checkable
class InvitationConsumptionRecoveryStore(Protocol):
    """Optional read-only capability, separate from same-process consumption."""

    def recover_invitation_consumption(
        self,
        recovery: InvitationConsumptionRecovery,
        *,
        token: str,
        trusted_attempt: InvitationRecoveryBinding,
    ) -> InvitationConsumptionReadback: ...
