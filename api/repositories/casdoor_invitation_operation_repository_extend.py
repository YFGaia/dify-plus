"""Exact pending invitation operations in a caller-owned SQL root.

The service owns protocol verification handoffs, fresh P1 observation outside
SQL, commit acknowledgement and fresh-session readback. This repository never
commits, retries, opens a savepoint or performs Redis I/O. Every failure requires
whole-root rollback. SQLite verifies sequential state only; NOWAIT contention
and dialect behavior require separate PostgreSQL/MySQL acceptance.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from time import monotonic
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.casdoor.admission import AdmissionContext
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_account_preflight_repository_extend import _validate as validate_attempt_context
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.invitation_authority_repository_extend import _payload as parse_issuance_payload
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import VersionedInvitationObservation

_IDEMPOTENCY_DOMAIN = b"casdoor:invitation-finalize:operation:v1:"
_RECONCILE_WINDOW = timedelta(days=7)
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "integration_id",
        "namespace_id",
        "revision_id",
        "config_digest",
        "identity_id",
        "subject_digest",
        "account_id",
        "workspace_id",
        "generation",
        "fence_epoch",
        "issuance_id",
        "payload_digest",
        "lifecycle_id",
        "lifecycle_epoch",
        "join_id_at_issue",
        "observed_join_role",
        "operation_id",
        "expected_receipt_json",
        "reconcile_until_utc",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "operation_id",
        "issuance_id",
        "lifecycle_id",
        "lifecycle_epoch",
        "account_id",
        "workspace_id",
        "join_id_at_issue",
        "token_digest",
        "payload_digest",
        "key_digest",
    }
)


class InvitationOperationConflict(ValueError):
    def __init__(self):
        super().__init__("invitation_operation_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class InvitationOperationAttempt:
    """Trusted internal selectors from verified claims; this DTO authenticates nothing."""

    context: AdmissionContext
    key: VerifiedIdentityKey
    account_id: UUID
    workspace_id: UUID
    issuance_id: UUID
    expected_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class InvitationOperationSnapshot:
    """SQL facts only; this repository cannot assert commit acknowledgement."""

    operation_id: UUID
    desired_json: str
    scope_digest: str
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class InvitationIssuanceFacts:
    payload_json: str
    payload_digest: str
    observed_join_role: str | None


@dataclass(frozen=True, slots=True, repr=False)
class _Scope:
    identity: Identity | None
    issuance: Issuance
    lifecycle: Lifecycle
    join: TenantAccountJoin | None
    facts: InvitationIssuanceFacts


def _require(condition):
    if not condition:
        raise InvitationOperationConflict()


def _uuid(value, *, version4=False):
    _require(type(value) is str and len(value) == 36)
    try:
        result = UUID(value)
    except ValueError:
        raise InvitationOperationConflict() from None
    _require(str(result) == value and (not version4 or result.version == 4))
    return result


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _strict_json(raw, fields):
    _require(type(raw) is str and len(raw.encode("utf-8")) <= 16384)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _require(False))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvitationOperationConflict() from None
    _require(type(value) is dict and set(value) == fields and raw == _canonical(value))
    return value


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _deadline(created_at):
    _require(type(created_at) is datetime and created_at.tzinfo is None)
    return (
        (created_at + _RECONCILE_WINDOW).replace(tzinfo=UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def operation_idempotency_key(issuance_id: UUID) -> str:
    _require(type(issuance_id) is UUID)
    return sha256(_IDEMPOTENCY_DOMAIN + str(issuance_id).encode("ascii")).hexdigest()


def _attempt(value):
    _require(type(value) is InvitationOperationAttempt)
    validate_attempt_context(value.context, value.key, value.account_id, None)
    _require(type(value.workspace_id) is UUID and type(value.issuance_id) is UUID)
    _require(type(value.expected_generation) is int and 0 <= value.expected_generation <= 2**63 - 1)


class CasdoorInvitationOperationRepository:
    def __init__(self, session: Session):
        self._session = session

    def _root(self):
        session = self._session
        _require(isinstance(session, Session))
        transaction = session.get_transaction()
        _require(transaction is not None and transaction.is_active and session.is_active)
        _require(not session.in_nested_transaction() and not session.new and not session.dirty and not session.deleted)

    def _one(self, model, *conditions):
        """Current bounded locking read; abort on NOWAIT contention, never retry."""
        return self._session.scalar(
            sa.select(model).where(*conditions).execution_options(populate_existing=True).with_for_update(nowait=True)
        )

    def _scope(self, attempt):
        self._root()
        _attempt(attempt)
        context, key = attempt.context, attempt.key
        with self._session.no_autoflush:
            integration = self._one(Integration, Integration.id == str(context.integration_id))
            _require(
                integration is not None
                and integration.enabled is True
                and integration.active_revision_id == str(context.revision_id)
            )
            namespace = self._one(Namespace, Namespace.id == str(context.namespace_id))
            chain = (
                str(context.integration_id),
                context.issuer,
                context.organization,
                context.application,
                context.client_id,
            )
            _require(
                namespace is not None
                and (
                    namespace.integration_id,
                    namespace.expected_issuer,
                    namespace.organization,
                    namespace.application,
                    namespace.client_id,
                    namespace.lifecycle,
                    namespace.fence_epoch,
                )
                == (*chain, CasdoorNamespaceLifecycle.ACTIVE, context.fence_epoch)
            )
            revision = self._one(Revision, Revision.id == str(context.revision_id))
            _require(
                revision is not None
                and (
                    revision.integration_id,
                    revision.expected_issuer,
                    revision.organization,
                    revision.application,
                    revision.client_id,
                    revision.namespace_id,
                    revision.config_digest,
                )
                == (*chain, str(context.namespace_id), context.config_digest)
            )
            account = self._one(Account, Account.id == str(attempt.account_id))
            _require(
                account is not None
                and account.status in (AccountStatus.ACTIVE, AccountStatus.PENDING, AccountStatus.UNINITIALIZED)
            )
            workspace = self._one(Tenant, Tenant.id == str(attempt.workspace_id))
            _require(workspace is not None and workspace.status == TenantStatus.NORMAL)
            identities = list(
                self._session.scalars(
                    sa.select(Identity)
                    .where(
                        Identity.namespace_id == str(context.namespace_id),
                        sa.or_(
                            Identity.subject_digest == key.subject_digest,
                            Identity.account_id == str(attempt.account_id),
                        ),
                    )
                    .order_by(Identity.id)
                    .limit(2)
                    .execution_options(populate_existing=True)
                    .with_for_update(nowait=True)
                )
            )
            _require(len(identities) <= 1)
            identity = identities[0] if identities else None
            if identity is not None:
                _uuid(identity.id)
                _require(
                    (
                        identity.namespace_id,
                        identity.issuer,
                        identity.organization,
                        identity.subject,
                        identity.subject_digest,
                        identity.account_id,
                        identity.sync_generation,
                    )
                    == (
                        str(key.namespace_id),
                        key.issuer,
                        key.organization,
                        key.subject,
                        key.subject_digest,
                        str(attempt.account_id),
                        attempt.expected_generation,
                    )
                )
            else:
                _require(attempt.expected_generation == 0)
            lifecycle = self._one(
                Lifecycle,
                Lifecycle.account_id == str(attempt.account_id),
                Lifecycle.workspace_id == str(attempt.workspace_id),
            )
            _require(lifecycle is not None and lifecycle.state == "active")
            issuance = self._one(Issuance, Issuance.issuance_id == str(attempt.issuance_id))
            _require(issuance is not None)
            data = parse_issuance_payload(issuance.payload_json)
            authority = data["invitation_authority"]
            _require(account.email == data["email"])
            _require(sha256(issuance.payload_json.encode()).hexdigest() == issuance.payload_digest)
            _require(
                (issuance.account_id, issuance.workspace_id, issuance.email, issuance.role, issuance.requires_setup)
                == (
                    str(attempt.account_id),
                    str(attempt.workspace_id),
                    data["email"],
                    data["role"],
                    data["requires_setup"],
                )
            )
            _require((data["account_id"], data["workspace_id"]) == (str(attempt.account_id), str(attempt.workspace_id)))
            _require(
                (
                    issuance.issuance_id,
                    issuance.lifecycle_id,
                    issuance.lifecycle_epoch,
                    issuance.token_digest,
                    issuance.join_id_at_issue,
                )
                == (
                    authority["issuance_id"],
                    authority["lifecycle_id"],
                    authority["lifecycle_epoch"],
                    authority["token_digest"],
                    authority["join_id_at_issue"],
                )
            )
            _require((lifecycle.lifecycle_id, lifecycle.epoch) == (issuance.lifecycle_id, issuance.lifecycle_epoch))
            _require(
                issuance.state == "issued"
                and issuance.consumption_receipt_json is None
                and issuance.consumed_at is None
            )
            joins = list(
                self._session.scalars(
                    sa.select(TenantAccountJoin)
                    .where(
                        TenantAccountJoin.account_id == str(attempt.account_id),
                        TenantAccountJoin.tenant_id == str(attempt.workspace_id),
                    )
                    .limit(2)
                    .execution_options(populate_existing=True)
                    .with_for_update(nowait=True)
                )
            )
            _require(len(joins) <= 1)
            join = joins[0] if joins else None
            _require((join.id if join else None) == issuance.join_id_at_issue)
            role = join.role if join else None
            _require(role is None or type(role) in (str, TenantAccountRole))
            role = role.value if type(role) is TenantAccountRole else role
            _require(role is None or 0 < len(role.encode()) <= 255)
            facts = InvitationIssuanceFacts(issuance.payload_json, issuance.payload_digest, role)
            return _Scope(identity, issuance, lifecycle, join, facts)

    @staticmethod
    def _fields(attempt, scope, *, operation_id, receipt, created_at):
        _require(scope.identity is not None)
        context, issuance = attempt.context, scope.issuance
        return {
            "schema_version": 1,
            "kind": "invitation_finalize",
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": str(context.revision_id),
            "config_digest": context.config_digest,
            "identity_id": scope.identity.id,
            "subject_digest": attempt.key.subject_digest,
            "account_id": issuance.account_id,
            "workspace_id": issuance.workspace_id,
            "generation": scope.identity.sync_generation,
            "fence_epoch": context.fence_epoch,
            "issuance_id": issuance.issuance_id,
            "payload_digest": issuance.payload_digest,
            "lifecycle_id": issuance.lifecycle_id,
            "lifecycle_epoch": issuance.lifecycle_epoch,
            "join_id_at_issue": issuance.join_id_at_issue,
            "observed_join_role": scope.facts.observed_join_role,
            "operation_id": operation_id,
            "expected_receipt_json": receipt,
            "reconcile_until_utc": _deadline(created_at),
        }

    def _snapshot(self, row, attempt, scope):
        data = _strict_json(row.desired_json, _SNAPSHOT_FIELDS)
        receipt = _strict_json(data["expected_receipt_json"], _RECEIPT_FIELDS)
        _uuid(row.id, version4=True)
        _digest(receipt["key_digest"])
        expected_receipt = {
            "schema_version": 1,
            "status": "consumed",
            "operation_id": row.id,
            "issuance_id": scope.issuance.issuance_id,
            "lifecycle_id": scope.issuance.lifecycle_id,
            "lifecycle_epoch": scope.issuance.lifecycle_epoch,
            "account_id": scope.issuance.account_id,
            "workspace_id": scope.issuance.workspace_id,
            "join_id_at_issue": scope.issuance.join_id_at_issue,
            "token_digest": scope.issuance.token_digest,
            "payload_digest": scope.issuance.payload_digest,
            "key_digest": receipt["key_digest"],
        }
        # Canonical bytes distinguish booleans from integers and reject duplicate fields.
        _require(data["expected_receipt_json"] == _canonical(expected_receipt))
        expected = self._fields(
            attempt, scope, operation_id=row.id, receipt=data["expected_receipt_json"], created_at=row.created_at
        )
        _require(row.desired_json == _canonical(expected))
        _require(row.scope_digest == sha256(row.desired_json.encode()).hexdigest())
        _require(row.idempotency_key == operation_idempotency_key(attempt.issuance_id))
        _require(
            (
                row.namespace_id,
                row.identity_id,
                row.account_id,
                row.workspace_id,
                row.revision_id,
                row.generation,
                row.fence_epoch,
            )
            == (
                data["namespace_id"],
                data["identity_id"],
                data["account_id"],
                data["workspace_id"],
                data["revision_id"],
                data["generation"],
                data["fence_epoch"],
            )
        )
        _require(
            row.kind == CasdoorIntentKind.INVITATION_FINALIZE and row.membership_id is None and row.ownership_epoch == 0
        )
        _require(
            row.operation_state == CasdoorOperationState.PENDING
            and row.termination_state == CasdoorTerminationState.NOT_STARTED
        )
        _require(row.attempt_count == 0 and row.resource_type is None and row.resource_id is None)
        _require(
            all(
                getattr(row, name) is None
                for name in (
                    "attempt_id",
                    "lease_owner",
                    "lease_expires_at",
                    "sent_at",
                    "acknowledged_at",
                    "readback_at",
                    "terminated_at",
                    "termination_proof_kind",
                    "proof_ref",
                    "retry_at",
                    "error_code",
                )
            )
        )
        _require(row.created_at <= _now() < row.created_at + _RECONCILE_WINDOW)
        return InvitationOperationSnapshot(
            UUID(row.id), row.desired_json, row.scope_digest, row.idempotency_key, row.created_at
        )

    def inspect(self, attempt: InvitationOperationAttempt):
        """Revalidate current facts and the sole operation for this issuance."""
        scope = self._scope(attempt)
        row = self._one(Intent, Intent.idempotency_key == operation_idempotency_key(attempt.issuance_id))
        return (self._snapshot(row, attempt, scope) if row else None), scope.facts

    def create_pending(self, attempt, *, store, observation, observation_deadline, expected_facts):
        """Internal creation from a fresh authentic P1 observation; no token I/O.

        The service supplies the conservatively measured observation deadline.
        _consumption_arguments checks the unchanged P1 issuing-store registry and
        reads only the local configured prefix; it sends no Redis command.
        """
        scope = self._scope(attempt)
        _require(scope.identity is not None and scope.facts == expected_facts)
        _require(type(store) is RedisInvitationTokenStore and type(observation) is VersionedInvitationObservation)
        _require(type(observation_deadline) is float and isfinite(observation_deadline))
        _require(0 < observation_deadline - monotonic() <= 30.0)
        _require(
            observation._raw == scope.issuance.payload_json.encode()
            and observation.payload_digest == scope.issuance.payload_digest
        )
        _require(
            (observation.payload.account_id, observation.payload.workspace_id)
            == (scope.issuance.account_id, scope.issuance.workspace_id)
        )
        existing = self._one(Intent, Intent.idempotency_key == operation_idempotency_key(attempt.issuance_id))
        if existing is not None:
            return self._snapshot(existing, attempt, scope)
        operation_id, created_at = str(uuid4()), _now()
        _keys, receipt = store._consumption_arguments(observation, operation_id)
        _require(monotonic() < observation_deadline)
        desired = _canonical(
            self._fields(attempt, scope, operation_id=operation_id, receipt=receipt.decode(), created_at=created_at)
        )
        row = Intent(
            id=operation_id,
            namespace_id=str(attempt.context.namespace_id),
            identity_id=scope.identity.id,
            account_id=str(attempt.account_id),
            workspace_id=str(attempt.workspace_id),
            membership_id=None,
            revision_id=str(attempt.context.revision_id),
            generation=scope.identity.sync_generation,
            ownership_epoch=0,
            fence_epoch=attempt.context.fence_epoch,
            kind=CasdoorIntentKind.INVITATION_FINALIZE,
            scope_digest=sha256(desired.encode()).hexdigest(),
            idempotency_key=operation_idempotency_key(attempt.issuance_id),
            desired_json=desired,
            operation_state=CasdoorOperationState.PENDING,
            termination_state=CasdoorTerminationState.NOT_STARTED,
            created_at=created_at,
            updated_at=created_at,
        )
        self._session.add(row)
        self._session.flush([row])
        _require(monotonic() < observation_deadline)
        return self._snapshot(row, attempt, scope)
