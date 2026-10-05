"""Same-root completed invitation observation and candidate LOCAL lease scope.

The attempt and returned facts authenticate nothing. No token/receipt store,
writer, intent exemption, lease acquisition or session issuance lives here.
P3L owns its precise existing NOWAIT chain; expanded discovery adds no locks
and makes no global-snapshot or missing-row serialization claim.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from models.account import Account, AccountIntegrate, AccountStatus, TenantStatus
from models.account import TenantAccountRole
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from repositories.casdoor_account_preflight_repository_extend import _validate as validate_recovery_context
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
    InvitationFinalizationFacts,
    _new_role,
    _proof,
    local_policy,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    CasdoorInvitationOperationRepository,
    _SNAPSHOT_FIELDS,
    InvitationOperationAttempt,
    _attempt,
    _strict_json,
    InvitationOperationSnapshot,
    _canonical,
    _deadline,
    _now,
    _RECONCILE_WINDOW,
    _uuid,
    operation_idempotency_key,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
    LoginScope,
)
from repositories.invitation_authority_repository_extend import _payload
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityRepository,
    InvitationIssuanceRecord,
    MAX_LIFECYCLE_EPOCH,
    _receipt,
)
from services.entities.account_activation_entities import VersionedInvitationObservation


@dataclass(frozen=True, slots=True, repr=False)
class CompletedInvitationLoginScope:
    """Current read observation only; no admission or future-writer capability."""

    attempt: InvitationOperationAttempt
    completion: InvitationFinalizationFacts
    scope: LoginScope


@dataclass(frozen=True, slots=True, repr=False)
class UnconsumedInvitationLoginScope:
    """Current discovery only; the actual issuing store/caller owns provenance."""

    attempt: InvitationOperationAttempt
    scope: LoginScope
    issuance: tuple
    lifecycle: tuple
    quota: tuple
    credentials_sha256: str
    legacy_links_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class InvitationRecoveryLoginScope:
    """Unlocked current SQL candidate, never a continuation or session authority.

    Phase facts only select an existing owner. Fresh locked proofs, current
    signed roles/admission and the actual caller's private capability are still
    required before any effect. Missing quota is deliberately retained.
    """

    attempt: InvitationOperationAttempt
    snapshot: InvitationOperationSnapshot
    scope: LoginScope
    issuance: InvitationIssuanceRecord
    lifecycle: tuple
    phase: Literal["pending", "invitation_completed", "memberships_written", "finalized"]
    quota: tuple
    credentials_sha256: str
    legacy_links_sha256: str


class CasdoorInvitedLoginScopeRepository:
    def __init__(
        self,
        session: Session,
        configuration_factory: Callable[[Session], CasdoorConfigurationRepository],
    ):
        self.session = session
        self.configuration_factory = configuration_factory

    def _recovery_rows(self, model, *conditions, cap=1, text_limit=16384):
        """Nonlocking count/byte headers before bounded current scalar contents."""
        table = model.__table__
        keys = tuple(table.primary_key.columns)
        texts = tuple(column for column in table.columns if isinstance(column.type, sa.Text))
        lengths = tuple(
            sa.func.length(sa.cast(column, sa.LargeBinary))
            if self.session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(column)
            for column in texts
        )
        headers = tuple(
            self.session.execute(sa.select(*keys, *lengths).where(*conditions).order_by(*keys).limit(cap + 1))
        )
        if len(headers) > cap or any(
            size is not None and (type(size) is not int or not 0 <= size <= text_limit)
            for row in headers
            for size in tuple(row)[len(keys) :]
        ):
            raise CasdoorLoginScopeConflict()
        rows = tuple(
            self.session.execute(
                sa.select(*table.columns)
                .where(
                    *conditions,
                    *(
                        sa.or_(column.is_(None), length <= text_limit)
                        for column, length in zip(texts, lengths, strict=True)
                    ),
                )
                .order_by(*keys)
                .limit(cap + 1)
            )
        )
        if len(rows) != len(headers) or tuple(
            tuple(getattr(row, column.name) for column in keys) for row in rows
        ) != tuple(tuple(row)[: len(keys)] for row in headers):
            raise CasdoorLoginScopeConflict()
        return rows

    def _discover_invitation_recovery(self, context, key, *, token_digest, remote_email=None):
        """Resolve only original digest -> issuance -> deterministic operation.

        The actual native-verifying caller owns authentication. This SQL-only
        discovery takes no locks, consumes nothing, and confers no capability.
        A missing original operation never grants a recovery continuation.
        """
        projection = CasdoorLoginScopeRepository(self.session, self.configuration_factory)
        projection._clean()
        try:
            validate_recovery_context(context, key, None, remote_email)
            local_policy()
            with self.session.no_autoflush:
                issuance = InvitationAuthorityRepository().get_issuance_by_token_digest(
                    self.session, token_digest=token_digest
                )
                if issuance is None:
                    return None
                payload = _payload(issuance.payload_json)
                authority = payload["invitation_authority"]
                issuance_id = UUID(issuance.issuance_id)
                operations = self._recovery_rows(
                    Intent, Intent.idempotency_key == operation_idempotency_key(issuance_id)
                )
                if not operations:
                    return None
                operation = operations[0]
                data = _strict_json(operation.desired_json, _SNAPSHOT_FIELDS)
                receipt = _receipt(data["expected_receipt_json"])
                _uuid(operation.id, version4=True)
                _uuid(data["identity_id"])
                attempt = InvitationOperationAttempt(
                    context,
                    key,
                    UUID(payload["account_id"]),
                    UUID(payload["workspace_id"]),
                    issuance_id,
                    data["generation"],
                )
                _attempt(attempt)
                scope = projection._project_scope(
                    context, key, attempt.account_id, creation_email=None, extra_workspace_ids=(attempt.workspace_id,)
                )
                if remote_email is not None:
                    scope = replace(
                        scope,
                        lease_scope=replace(
                            scope.lease_scope, emails=tuple(sorted(set(scope.lease_scope.emails) | {remote_email}))
                        ),
                    )
                if (
                    scope.account is None
                    or scope.account.id != payload["account_id"]
                    or scope.account.email != payload["email"]
                    or scope.account.status is not AccountStatus.ACTIVE
                    or type(scope.account.initialized_at) is not datetime
                    or scope.account.initialized_at > _now()
                    or len(scope.identities) != 1
                    or len(scope.intents) != 1
                    or scope.intents[0].id != operation.id
                    or any(row.status is not TenantStatus.NORMAL for row in scope.workspaces)
                ):
                    raise CasdoorLoginScopeConflict()
                identity = scope.identities[0]
                if tuple(identity)[:7] != (
                    data["identity_id"],
                    str(key.namespace_id),
                    payload["account_id"],
                    key.issuer,
                    key.organization,
                    key.subject,
                    key.subject_digest,
                ):
                    raise CasdoorLoginScopeConflict()
                expected_receipt = {
                    "schema_version": 1,
                    "status": "consumed",
                    "operation_id": operation.id,
                    "issuance_id": issuance.issuance_id,
                    "lifecycle_id": authority["lifecycle_id"],
                    "lifecycle_epoch": authority["lifecycle_epoch"],
                    "account_id": payload["account_id"],
                    "workspace_id": payload["workspace_id"],
                    "join_id_at_issue": authority["join_id_at_issue"],
                    "token_digest": authority["token_digest"],
                    "payload_digest": issuance.payload_digest,
                    "key_digest": receipt["key_digest"],
                }
                expected = {
                    "schema_version": 1,
                    "kind": "invitation_finalize",
                    "integration_id": str(context.integration_id),
                    "namespace_id": str(context.namespace_id),
                    "revision_id": str(context.revision_id),
                    "config_digest": context.config_digest,
                    "identity_id": identity.id,
                    "subject_digest": key.subject_digest,
                    "account_id": payload["account_id"],
                    "workspace_id": payload["workspace_id"],
                    "generation": attempt.expected_generation,
                    "fence_epoch": context.fence_epoch,
                    "issuance_id": issuance.issuance_id,
                    "payload_digest": issuance.payload_digest,
                    "lifecycle_id": authority["lifecycle_id"],
                    "lifecycle_epoch": authority["lifecycle_epoch"],
                    "join_id_at_issue": authority["join_id_at_issue"],
                    "observed_join_role": data["observed_join_role"],
                    "operation_id": operation.id,
                    "expected_receipt_json": _canonical(expected_receipt),
                    "reconcile_until_utc": _deadline(operation.created_at),
                }
                if (
                    operation.desired_json != _canonical(expected)
                    or operation.scope_digest != sha256(operation.desired_json.encode()).hexdigest()
                    or any(
                        getattr(operation, field) != expected[field]
                        for field in (
                            "namespace_id",
                            "identity_id",
                            "account_id",
                            "workspace_id",
                            "revision_id",
                            "generation",
                            "fence_epoch",
                        )
                    )
                    or operation.kind is not CasdoorIntentKind.INVITATION_FINALIZE
                    or operation.membership_id is not None
                    or operation.ownership_epoch != 0
                    or operation.attempt_count != 0
                    or any(
                        getattr(operation, field) is not None
                        for field in (
                            "resource_type",
                            "resource_id",
                            "attempt_id",
                            "lease_owner",
                            "lease_expires_at",
                            "sent_at",
                            "acknowledged_at",
                            "readback_at",
                            "retry_at",
                            "error_code",
                        )
                    )
                    or not operation.created_at <= _now() < operation.created_at + _RECONCILE_WINDOW
                    or tuple(scope.intents[0])
                    != tuple(
                        getattr(operation, field)
                        for field in (
                            "id",
                            "namespace_id",
                            "identity_id",
                            "account_id",
                            "workspace_id",
                            "membership_id",
                            "revision_id",
                            "kind",
                            "generation",
                            "ownership_epoch",
                            "fence_epoch",
                            "operation_state",
                            "termination_state",
                        )
                    )
                ):
                    raise CasdoorLoginScopeConflict()
                snapshot = InvitationOperationSnapshot(
                    UUID(operation.id),
                    operation.desired_json,
                    operation.scope_digest,
                    operation.idempotency_key,
                    operation.created_at,
                )
                lifecycle = self._recovery_rows(
                    Lifecycle,
                    Lifecycle.account_id == payload["account_id"],
                    Lifecycle.workspace_id == payload["workspace_id"],
                )
                if (
                    len(lifecycle) != 1
                    or lifecycle[0].lifecycle_id != authority["lifecycle_id"]
                    or lifecycle[0].state != "active"
                ):
                    raise CasdoorLoginScopeConflict()
                phase = self._recovery_phase(attempt, scope, snapshot, operation, issuance, lifecycle[0], data)
                quota = self._recovery_rows(AccountMoneyExtend, AccountMoneyExtend.account_id == payload["account_id"])
                credentials = projection._rows(
                    sa.select(Account.password, Account.password_salt).where(Account.id == payload["account_id"]), cap=1
                )
                if len(credentials) != 1:
                    raise CasdoorLoginScopeConflict()
                links = self._recovery_rows(
                    AccountIntegrate, AccountIntegrate.account_id == payload["account_id"], cap=2048
                )
                credential_digest = sha256(
                    b"casdoor:invited-account-credential-preservation:v1:"
                    + payload["account_id"].encode()
                    + json.dumps(tuple(credentials[0]), separators=(",", ":")).encode()
                ).hexdigest()
                links_digest = sha256(
                    b"casdoor:invited-account-legacy-links-preservation:v1:"
                    + payload["account_id"].encode()
                    + json.dumps(tuple(tuple(row) for row in links), default=str, separators=(",", ":")).encode()
                ).hexdigest()
                return InvitationRecoveryLoginScope(
                    attempt, snapshot, scope, issuance, lifecycle, phase, quota, credential_digest, links_digest
                )
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            raise CasdoorLoginScopeConflict() from None

    def _recovery_phase(self, attempt, scope, snapshot, operation, issuance, lifecycle, data):
        """Original P3L proof and current G/G+1 SQL phase, without replay."""
        from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as f_kind
        from core.casdoor.invited_write_receipt import RECEIPT_KIND as d_kind

        audits = self._recovery_rows(
            Audit, Audit.correlation_id == operation.id, Audit.action.in_((d_kind, f_kind)), cap=2, text_limit=32768
        )
        d_rows = tuple(row for row in audits if row.action == d_kind)
        f_rows = tuple(row for row in audits if row.action == f_kind)
        if len(d_rows) > 1 or len(f_rows) > 1:
            raise CasdoorLoginScopeConflict()
        joins = tuple(row for row in scope.joins if row.tenant_id == str(attempt.workspace_id))
        if len(joins) > 1:
            raise CasdoorLoginScopeConflict()
        join = joins[0] if joins else None
        generation = scope.identities[0].sync_generation
        if operation.operation_state is CasdoorOperationState.PENDING:
            if (
                operation.termination_state is not CasdoorTerminationState.NOT_STARTED
                or any(
                    getattr(operation, field) is not None
                    for field in ("terminated_at", "termination_proof_kind", "proof_ref")
                )
                or operation.updated_at != operation.created_at
                or issuance.state != "issued"
                or lifecycle.epoch != data["lifecycle_epoch"]
                or generation != attempt.expected_generation
                or (join.id if join else None) != data["join_id_at_issue"]
                or (join.role if join else None) != data["observed_join_role"]
                or audits
            ):
                raise CasdoorLoginScopeConflict()
            if join is None:
                _new_role(_payload(issuance.payload_json)["role"])
                if lifecycle.epoch == MAX_LIFECYCLE_EPOCH:
                    raise CasdoorLoginScopeConflict()
            from repositories.casdoor_invited_existing_history_repository_extend import prior_history_rows

            prior_history_rows(
                self.session,
                scope,
                namespace_id=attempt.key.namespace_id,
                account_id=attempt.account_id,
                identity_id=UUID(scope.identities[0].id),
                generation=generation,
            )
            return "pending"
        if (
            operation.operation_state is not CasdoorOperationState.APPLIED
            or operation.termination_state is not CasdoorTerminationState.CONFIRMED
            or operation.termination_proof_kind != "invitation_finalize_sql_v1"
            or type(operation.proof_ref) is not str
            or len(operation.proof_ref.encode()) > 128
        ):
            raise CasdoorLoginScopeConflict()
        match = re.fullmatch(r"v1:([0-9a-f-]{36}):([01]):([1-9][0-9]{0,15}):([0-9a-f]{64})", operation.proof_ref)
        if match is None:
            raise CasdoorLoginScopeConflict()
        join_id, created_text, epoch_text, _ = match.groups()
        _uuid(join_id)
        created, result_epoch = created_text == "1", int(epoch_text)
        role = data["observed_join_role"] if not created else _payload(issuance.payload_json)["role"]
        if (
            created != (data["join_id_at_issue"] is None)
            or result_epoch > MAX_LIFECYCLE_EPOCH
            or result_epoch != data["lifecycle_epoch"] + int(created)
            or lifecycle.epoch != result_epoch
            or join is None
            or join.id != join_id
            or join.role != role
            or created
            and (role not in {item.value for item in TenantAccountRole} or role == TenantAccountRole.OWNER)
            or (not created and join_id != data["join_id_at_issue"])
            or issuance.state != "consumed"
            or issuance.consumption_receipt_json != data["expected_receipt_json"]
            or type(operation.terminated_at) is not datetime
            or operation.terminated_at.tzinfo is not None
            or not operation.created_at <= operation.terminated_at < operation.created_at + _RECONCILE_WINDOW
            or operation.terminated_at != operation.updated_at
            or operation.terminated_at > _now()
            or not operation.created_at <= issuance.consumed_at <= operation.terminated_at
            or operation.proof_ref
            != _proof(snapshot, data, join_id=join_id, join_role=role, created=created, result_epoch=result_epoch)
        ):
            raise CasdoorLoginScopeConflict()
        if generation == attempt.expected_generation:
            if audits:
                raise CasdoorLoginScopeConflict()
            from repositories.casdoor_invited_existing_history_repository_extend import prior_history_rows

            prior_history_rows(
                self.session,
                scope,
                namespace_id=attempt.key.namespace_id,
                account_id=attempt.account_id,
                identity_id=UUID(scope.identities[0].id),
                generation=generation,
            )
            return "invitation_completed"
        if generation != attempt.expected_generation + 1 or len(d_rows) != 1:
            raise CasdoorLoginScopeConflict()
        self._recovery_postwrite(attempt, scope, snapshot, d_rows[0], f_rows[0] if f_rows else None)
        return "finalized" if f_rows else "memberships_written"

    def _recovery_postwrite(self, attempt, scope, snapshot, d_audit, f_audit):
        """Reuse original D verification; F facts still require fresh signed-plan F2.

        Deferred imports avoid the original readers' dependency on this scope
        module. The original immutable pending projection is not an ORM change.
        No first-SQL lock owner or writer is invoked by candidate discovery.
        """
        from core.casdoor.invited_finalization_receipt import (
            FULL_HISTORY_FIELDS,
            InvitedLocalFinalizationReceipt,
            finalized_rows_sha256,
        )
        from core.casdoor.invited_write_receipt import IDENTITY_FIELDS, JOIN_FIELDS, MEMBERSHIP_FIELDS, postwrite_sha256
        from core.casdoor.invited_controlled_write_receipt import controlled_finalization_ids
        from repositories.casdoor_invited_finalization_receipt_repository_extend import _pending, _primitive
        from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository

        reader = CasdoorInvitedWriteReceiptRepository(self.session, self.configuration_factory)
        rows = reader._rows(attempt, lock=False)
        current = reader._scope(CasdoorLoginScopeRepository(self.session, self.configuration_factory), attempt)
        if rows[4] != d_audit or replace(current, lease_scope=scope.lease_scope) != scope:
            raise CasdoorLoginScopeConflict()
        if f_audit is None:
            if reader._invitation(attempt, scope, rows) != snapshot:
                raise CasdoorLoginScopeConflict()
            reader._receipt(attempt, scope, rows, snapshot)
            return
        f = InvitedLocalFinalizationReceipt(f_audit.summary_json).values()
        ids = frozenset(f["finalized_ids"])
        pending_scope = replace(scope, histories=tuple(_pending(row, ids) for row in scope.histories))
        pending_rows = (*rows[:5], tuple(_pending(row, ids) for row in rows[5]))
        if reader._invitation(attempt, pending_scope, pending_rows) != snapshot:
            raise CasdoorLoginScopeConflict()
        d_receipt = reader._receipt(attempt, pending_scope, pending_rows, snapshot)
        d = d_receipt.values()
        if (
            not ids.issubset({row.id for row in rows[5]})
            or any(row.finalization.value != "finalized" for row in rows[5])
            or sorted(ids) != controlled_finalization_ids(d)
            or f["references"] != d["references"]
            or (f["generation"], f["fence_epoch"]) != (d["generation_after"], d["fence_epoch"])
            or f["write_receipt_sha256"] != sha256(d_receipt.canonical_json.encode()).hexdigest()
            or f["before_postwrite_sha256"] != d["postwrite_sha256"]
            or finalized_rows_sha256(
                str(snapshot.operation_id),
                tuple(_primitive(row, FULL_HISTORY_FIELDS) for row in rows[5] if row.id in ids),
            )
            != f["finalized_rows_sha256"]
            or postwrite_sha256(
                str(snapshot.operation_id),
                identity=_primitive(scope.identities[0], IDENTITY_FIELDS),
                joins=tuple(_primitive(row, JOIN_FIELDS) for row in scope.joins),
                memberships=tuple(_primitive(row, MEMBERSHIP_FIELDS) for row in scope.histories),
            )
            != f["postwrite_sha256"]
            or f_audit.actor_account_id is not None
            or f_audit.result_code != "verified"
            or not d_audit.created_at <= f_audit.created_at <= _now()
            or any(
                getattr(f_audit, field) != d["references"][field]
                for field in ("namespace_id", "revision_id", "identity_id", "account_id")
            )
        ):
            raise CasdoorLoginScopeConflict()

    def discover_unconsumed_invitation(self, context, key, observation, *, remote_email=None):
        """Discover exact P1/SQL authority and all parents before acquiring leases.

        This never resolves an account by email or takes row locks. The caller
        must already verify the original observation registry and signed claims.
        Completed/consumed invitations have their separate existing owners.
        """
        projection = CasdoorLoginScopeRepository(self.session, self.configuration_factory)
        projection._clean()
        if type(observation) is not VersionedInvitationObservation:
            raise CasdoorLoginScopeConflict()
        try:
            data = _payload(observation._raw.decode("utf-8"))
            authority = data["invitation_authority"]
            payload = observation.payload
            if (
                sha256(observation._raw).hexdigest() != observation.payload_digest
                or (
                    payload.account_id,
                    payload.workspace_id,
                    payload.email,
                    payload.role,
                    payload.requires_setup,
                )
                != tuple(
                    data[k]
                    for k in (
                        "account_id",
                        "workspace_id",
                        "email",
                        "role",
                        "requires_setup",
                    )
                )
                or payload.invitation_authority.issuance_id != authority["issuance_id"]
            ):
                raise CasdoorLoginScopeConflict()
            scope = projection._project_scope(
                context,
                key,
                UUID(data["account_id"]),
                creation_email=None,
                extra_workspace_ids=(UUID(data["workspace_id"]),),
            )
            if remote_email is not None:
                scope = replace(
                    scope,
                    lease_scope=replace(
                        scope.lease_scope,
                        emails=tuple(sorted(set(scope.lease_scope.emails) | {remote_email})),
                    ),
                )
            if (
                scope.account is None
                or scope.account.email != data["email"]
                or scope.account.status
                not in (
                    AccountStatus.ACTIVE,
                    AccountStatus.PENDING,
                    AccountStatus.UNINITIALIZED,
                )
                or len(scope.identities) > 1
            ):
                raise CasdoorLoginScopeConflict()
            generation = 0
            if scope.identities:
                identity = scope.identities[0]
                if (
                    identity.namespace_id,
                    identity.account_id,
                    identity.issuer,
                    identity.organization,
                    identity.subject,
                    identity.subject_digest,
                ) != (
                    str(key.namespace_id),
                    data["account_id"],
                    key.issuer,
                    key.organization,
                    key.subject,
                    key.subject_digest,
                ):
                    raise CasdoorLoginScopeConflict()
                generation = identity.sync_generation
                from repositories.casdoor_invited_existing_history_repository_extend import prior_history_rows

                prior_history_rows(
                    self.session,
                    scope,
                    namespace_id=key.namespace_id,
                    account_id=UUID(data["account_id"]),
                    identity_id=UUID(identity.id),
                    generation=generation,
                )
            elif scope.histories:
                raise CasdoorLoginScopeConflict()
            attempt = InvitationOperationAttempt(
                context,
                key,
                UUID(data["account_id"]),
                UUID(data["workspace_id"]),
                UUID(authority["issuance_id"]),
                generation,
            )
            _attempt(attempt)
            if scope.intents and (
                len(scope.intents) != 1
                or not scope.identities
                or tuple(scope.intents[0])[1:]
                != (
                    str(context.namespace_id),
                    scope.identities[0].id,
                    data["account_id"],
                    data["workspace_id"],
                    None,
                    str(context.revision_id),
                    CasdoorIntentKind.INVITATION_FINALIZE,
                    generation,
                    0,
                    context.fence_epoch,
                    CasdoorOperationState.PENDING,
                    CasdoorTerminationState.NOT_STARTED,
                )
            ):
                raise CasdoorLoginScopeConflict()
            issuance = projection._rows(
                sa.select(*Issuance.__table__.columns).where(Issuance.issuance_id == authority["issuance_id"]),
                cap=1,
            )
            lifecycle = projection._rows(
                sa.select(*Lifecycle.__table__.columns).where(
                    Lifecycle.account_id == data["account_id"],
                    Lifecycle.workspace_id == data["workspace_id"],
                ),
                cap=1,
            )
            if len(issuance) != 1 or len(lifecycle) != 1:
                raise CasdoorLoginScopeConflict()
            i, lifecycle_row = issuance[0], lifecycle[0]
            if (
                i.payload_json.encode("utf-8") != observation._raw
                or i.payload_digest != observation.payload_digest
                or i.state != "issued"
                or i.consumption_receipt_json is not None
                or i.consumed_at is not None
                or (i.account_id, i.workspace_id, i.email, i.role, i.requires_setup)
                != (
                    payload.account_id,
                    payload.workspace_id,
                    payload.email,
                    payload.role,
                    payload.requires_setup,
                )
                or (
                    i.lifecycle_id,
                    i.lifecycle_epoch,
                    i.token_digest,
                    i.join_id_at_issue,
                )
                != tuple(
                    authority[k]
                    for k in (
                        "lifecycle_id",
                        "lifecycle_epoch",
                        "token_digest",
                        "join_id_at_issue",
                    )
                )
                or (
                    lifecycle_row.lifecycle_id,
                    lifecycle_row.epoch,
                    lifecycle_row.state,
                )
                != (i.lifecycle_id, i.lifecycle_epoch, "active")
            ):
                raise CasdoorLoginScopeConflict()
            joins = tuple(r for r in scope.joins if r.tenant_id == data["workspace_id"])
            if len(joins) > 1 or (joins[0].id if joins else None) != i.join_id_at_issue:
                raise CasdoorLoginScopeConflict()
            quota = projection._rows(
                sa.select(*AccountMoneyExtend.__table__.columns).where(
                    AccountMoneyExtend.account_id == data["account_id"]
                ),
                cap=1,
            )
            credentials = projection._rows(
                sa.select(Account.password, Account.password_salt).where(Account.id == data["account_id"]),
                cap=1,
            )
            if len(credentials) != 1:
                raise CasdoorLoginScopeConflict()
            # Transient same-process integrity only. Neither stored credential
            # values nor this digest enters durable intent/audit/public output.
            credential_digest = sha256(
                b"casdoor:invited-account-credential-preservation:v1:"
                + data["account_id"].encode()
                + json.dumps(tuple(credentials[0]), separators=(",", ":")).encode()
            ).hexdigest()
            links = projection._rows(
                sa.select(*AccountIntegrate.__table__.columns)
                .where(AccountIntegrate.account_id == data["account_id"])
                .order_by(AccountIntegrate.id)
            )
            links_digest = sha256(
                b"casdoor:invited-account-legacy-links-preservation:v1:"
                + data["account_id"].encode()
                + json.dumps(
                    tuple(tuple(row) for row in links),
                    default=str,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            return UnconsumedInvitationLoginScope(
                attempt,
                scope,
                issuance,
                lifecycle,
                quota,
                credential_digest,
                links_digest,
            )
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            OverflowError,
        ):
            raise CasdoorLoginScopeConflict() from None

    def prelock_unconsumed_invitation(
        self, expected, observation, *, remote_email=None
    ):
        """First SQL owner in a fresh root; lock only the complete leased parents."""
        projection = CasdoorLoginScopeRepository(
            self.session, self.configuration_factory
        )
        projection._clean()
        root = self.session.get_transaction()
        if (
            type(expected) is not UnconsumedInvitationLoginScope
            or root.origin is not SessionTransactionOrigin.BEGIN
            or getattr(root, "_connections", None) != {}
        ):
            raise CasdoorLoginScopeConflict()
        a = expected.attempt

        def current():
            return self.discover_unconsumed_invitation(
                a.context, a.key, observation, remote_email=remote_email
            )

        if current() != expected:
            raise CasdoorLoginScopeConflict()
        projection._lock_invited_integration(a.context)
        if current() != expected:
            raise CasdoorLoginScopeConflict()
        projection._lock_invited_candidate_parents(a.context, expected.scope)
        # Original operation owner locks and validates issuance/lifecycle/join
        # and the exact canonical PENDING operation, never a public flag.
        operation, _facts = CasdoorInvitationOperationRepository(self.session).inspect(
            a
        )
        if (
            (operation is not None) != bool(expected.scope.intents)
            or (
                operation is not None
                and str(operation.operation_id) != expected.scope.intents[0].id
            )
            or current() != expected
        ):
            raise CasdoorLoginScopeConflict()
        return expected

    def discover_completed_invitation(
        self, attempt: InvitationOperationAttempt
    ) -> CompletedInvitationLoginScope:
        """Require real same-session completion, then preserve every scalar barrier."""
        projection = CasdoorLoginScopeRepository(
            self.session, self.configuration_factory
        )
        projection._clean()
        transaction = self.session.get_transaction()
        if (
            transaction is None
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or type(attempt) is not InvitationOperationAttempt
        ):
            raise CasdoorLoginScopeConflict()
        try:
            with self.session.no_autoflush:
                completion = CasdoorInvitationFinalizationRepository(
                    self.session
                ).inspect(attempt)
                if (
                    type(completion) is not InvitationFinalizationFacts
                    or completion.completed is not True
                ):
                    raise CasdoorLoginScopeConflict()
                scope = projection._project_scope(
                    attempt.context,
                    attempt.key,
                    attempt.account_id,
                    creation_email=None,
                    extra_workspace_ids=(attempt.workspace_id,),
                )
                self._binding(attempt, completion, scope)
                return CompletedInvitationLoginScope(attempt, completion, scope)
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
        ):
            raise CasdoorLoginScopeConflict() from None

    @staticmethod
    def _binding(
        attempt: InvitationOperationAttempt,
        completion: InvitationFinalizationFacts,
        scope: LoginScope,
    ):
        """Bind projected current scalars to the exact P3L-owned immutable facts."""
        data = _strict_json(completion.snapshot.desired_json, _SNAPSHOT_FIELDS)
        issuance = _payload(completion.issuance.payload_json)
        context = attempt.context
        expected = {
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": str(context.revision_id),
            "config_digest": context.config_digest,
            "subject_digest": attempt.key.subject_digest,
            "account_id": str(attempt.account_id),
            "workspace_id": str(attempt.workspace_id),
            "issuance_id": str(attempt.issuance_id),
            "generation": attempt.expected_generation,
            "fence_epoch": context.fence_epoch,
            "operation_id": str(completion.snapshot.operation_id),
            "payload_digest": completion.issuance.payload_digest,
        }
        if any(data[key] != value for key, value in expected.items()):
            raise CasdoorLoginScopeConflict()
        identities = [row for row in scope.identities if row.id == data["identity_id"]]
        joins = [row for row in scope.joins if row.id == completion.join_id]
        intents = [row for row in scope.intents if row.id == data["operation_id"]]
        if (
            scope.account is None
            or (scope.account.id, scope.account.email)
            != (str(attempt.account_id), issuance["email"])
            or len(identities) != 1
            or tuple(identities[0])[1:]
            != (
                str(context.namespace_id),
                str(attempt.account_id),
                attempt.key.issuer,
                attempt.key.organization,
                attempt.key.subject,
                attempt.key.subject_digest,
                attempt.expected_generation,
            )
            or len(joins) != 1
            or (joins[0].account_id, joins[0].tenant_id, joins[0].role)
            != (
                str(attempt.account_id),
                str(attempt.workspace_id),
                issuance["role"]
                if completion.membership_created
                else completion.issuance.observed_join_role,
            )
            or len(intents) != 1
            or tuple(intents[0])[1:]
            != (
                str(context.namespace_id),
                data["identity_id"],
                str(attempt.account_id),
                str(attempt.workspace_id),
                None,
                str(context.revision_id),
                "invitation_finalize",
                attempt.expected_generation,
                0,
                context.fence_epoch,
                "applied",
                "confirmed",
            )
            or str(attempt.workspace_id) not in {row.id for row in scope.workspaces}
        ):
            raise CasdoorLoginScopeConflict()

    def prelock_and_recheck_completed_invitation(
        self, attempt: InvitationOperationAttempt
    ) -> CompletedInvitationLoginScope:
        """First SQL owner in a fresh root; observe, serialize, prelock and recheck.

        Serialization is limited to same-integration Casdoor UoWs following the
        integration-first contract. This is never an admission/writer capability
        or a promise about non-cooperating owners, gaps or global snapshots.
        """
        projection = CasdoorLoginScopeRepository(
            self.session, self.configuration_factory
        )
        projection._clean()
        transaction = self.session.get_transaction()
        # api/uv.lock pins SQLAlchemy 2.0.49. Any enlisted connection means this
        # root already executed SQL (possibly P3M-A/P3L locks); never extend it.
        connections = getattr(transaction, "_connections", None)
        if (
            transaction is None
            or not transaction.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or type(connections) is not dict
            or connections
        ):
            raise CasdoorLoginScopeConflict()
        try:
            _attempt(attempt)
            with self.session.no_autoflush:
                candidate = self._invited_candidate(projection, attempt)
                projection._lock_invited_integration(attempt.context)
                if self._invited_candidate(projection, attempt) != candidate:
                    raise CasdoorLoginScopeConflict()
                projection._lock_invited_candidate_parents(attempt.context, candidate)
                completion = CasdoorInvitationFinalizationRepository(
                    self.session
                ).inspect(attempt)
                if (
                    type(completion) is not InvitationFinalizationFacts
                    or completion.completed is not True
                ):
                    raise CasdoorLoginScopeConflict()
                # Every parent lock is already held. A new parent is observed
                # without locking it and makes this complete comparison fail.
                current = self._invited_candidate(projection, attempt)
                if current != candidate:
                    raise CasdoorLoginScopeConflict()
                self._binding(attempt, completion, current)
                return CompletedInvitationLoginScope(attempt, completion, current)
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
        ):
            raise CasdoorLoginScopeConflict() from None

    @staticmethod
    def _invited_candidate(
        projection: CasdoorLoginScopeRepository, attempt: InvitationOperationAttempt
    ) -> LoginScope:
        """Full unlocked scope with existing authority parents present before P3L."""
        scope = projection._project_scope(
            attempt.context,
            attempt.key,
            attempt.account_id,
            creation_email=None,
            extra_workspace_ids=(attempt.workspace_id,),
        )
        own = [
            row
            for row in scope.identities
            if row.namespace_id == str(attempt.context.namespace_id)
        ]
        invited = [
            row for row in scope.workspaces if row.id == str(attempt.workspace_id)
        ]
        if (
            scope.account is None
            or scope.account.id != str(attempt.account_id)
            or scope.account.status
            not in (
                AccountStatus.ACTIVE,
                AccountStatus.PENDING,
                AccountStatus.UNINITIALIZED,
            )
            or len(own) != 1
            or tuple(own[0])[1:]
            != (
                str(attempt.key.namespace_id),
                str(attempt.account_id),
                attempt.key.issuer,
                attempt.key.organization,
                attempt.key.subject,
                attempt.key.subject_digest,
                attempt.expected_generation,
            )
            or len(invited) != 1
            or invited[0].status != TenantStatus.NORMAL
        ):
            raise CasdoorLoginScopeConflict()
        return scope
