"""Exact local invitation SQL completion in an explicit caller-owned root.

No Redis, commit, rollback, savepoint, admission or remote resource effects.
Every exception requires whole-root rollback. The separate completed reader
permits only this operation's exact one-epoch membership creation self-change.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from configs import dify_config
from enums import DeploymentEdition
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
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    _RECEIPT_FIELDS,
    _RECONCILE_WINDOW,
    _SNAPSHOT_FIELDS,
    InvitationIssuanceFacts,
    InvitationOperationSnapshot,
    _attempt,
    _canonical,
    _deadline,
    _digest,
    _now,
    _require,
    _strict_json,
    _uuid,
    operation_idempotency_key,
)
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityRepository,
)
from repositories.invitation_authority_repository_extend import (
    _payload as parse_issuance_payload,
)
from services.account_service import TenantService

_PROOF_KIND = "invitation_finalize_sql_v1"
_PROOF_DOMAIN = b"casdoor:invitation-finalize:sql-completion:v1:"


def local_policy():
    _require(dify_config.DEPLOYMENT_EDITION == DeploymentEdition.COMMUNITY and dify_config.RBAC_ENABLED is False)


def _new_role(role):
    _require(type(role) is str and role in {r.value for r in TenantAccountRole} and role != TenantAccountRole.OWNER)
    _require(role != TenantAccountRole.DATASET_OPERATOR or dify_config.DATASET_OPERATOR_ENABLED is True)
    return role


@dataclass(frozen=True, slots=True, repr=False)
class InvitationFinalizationFacts:
    snapshot: InvitationOperationSnapshot
    issuance: InvitationIssuanceFacts
    completed: bool
    proof_ref: str | None = None
    join_id: str | None = None
    membership_created: bool | None = None
    result_epoch: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class _Scope:
    identity: Identity
    issuance: Issuance
    lifecycle: Lifecycle
    join: TenantAccountJoin | None
    facts: InvitationIssuanceFacts
    account: Account
    workspace: Tenant


def _proof(snapshot, data, *, join_id, join_role, created, result_epoch):
    _uuid(join_id)
    _require(type(created) is bool and type(result_epoch) is int and 1 <= result_epoch <= MAX_LIFECYCLE_EPOCH)
    value = {
        "operation_id": str(snapshot.operation_id),
        "scope_digest": snapshot.scope_digest,
        "expected_receipt_sha256": sha256(data["expected_receipt_json"].encode()).hexdigest(),
        "issuance_id": data["issuance_id"],
        "lifecycle_id": data["lifecycle_id"],
        "input_epoch": data["lifecycle_epoch"],
        "result_epoch": result_epoch,
        "join_id": join_id,
        "join_role": join_role,
        "membership_created": created,
    }
    digest = sha256(_PROOF_DOMAIN + _canonical(value).encode("ascii")).hexdigest()
    ref = f"v1:{join_id}:{int(created)}:{result_epoch}:{digest}"
    _require(ref.isascii() and len(ref.encode("ascii")) <= 128)
    return ref


class CasdoorInvitationFinalizationRepository:
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
        local_policy()
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
            _require(identity is not None)
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
            _require(lifecycle.lifecycle_id == issuance.lifecycle_id)
            _require(type(lifecycle.epoch) is int and 1 <= lifecycle.epoch <= MAX_LIFECYCLE_EPOCH)
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
            role = join.role if join else None
            _require(role is None or type(role) in (str, TenantAccountRole))
            role = role.value if type(role) is TenantAccountRole else role
            _require(role is None or 0 < len(role.encode()) <= 255)
            facts = InvitationIssuanceFacts(
                issuance.payload_json, issuance.payload_digest, role if issuance.join_id_at_issue else None
            )
            return _Scope(identity, issuance, lifecycle, join, facts, account, workspace)

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
                    "retry_at",
                    "error_code",
                )
            )
        )
        _require(row.created_at <= _now())
        return InvitationOperationSnapshot(
            UUID(row.id), row.desired_json, row.scope_digest, row.idempotency_key, row.created_at
        )

    def _inspect(self, attempt):
        scope = self._scope(attempt)
        row = self._one(Intent, Intent.idempotency_key == operation_idempotency_key(attempt.issuance_id))
        _require(row is not None)
        snapshot = self._snapshot(row, attempt, scope)
        data = _strict_json(snapshot.desired_json, _SNAPSHOT_FIELDS)
        pending = (
            row.operation_state == CasdoorOperationState.PENDING
            and row.termination_state == CasdoorTerminationState.NOT_STARTED
        )
        if pending:
            _require(
                all(getattr(row, name) is None for name in ("terminated_at", "termination_proof_kind", "proof_ref"))
            )
            _require(row.updated_at == row.created_at)
            _require(scope.issuance.state == "issued" and scope.issuance.consumption_receipt_json is None)
            _require(scope.issuance.consumed_at is None and scope.lifecycle.epoch == data["lifecycle_epoch"])
            _require((scope.join.id if scope.join else None) == data["join_id_at_issue"])
            if scope.join is None:
                _new_role(scope.issuance.role)
                _require(scope.lifecycle.epoch < MAX_LIFECYCLE_EPOCH)
            _require(_now() < row.created_at + _RECONCILE_WINDOW)
            return InvitationFinalizationFacts(snapshot, scope.facts, False), row, scope
        _require(
            row.operation_state == CasdoorOperationState.APPLIED
            and row.termination_state == CasdoorTerminationState.CONFIRMED
            and row.termination_proof_kind == _PROOF_KIND
        )
        _require(type(row.proof_ref) is str and row.proof_ref.isascii() and len(row.proof_ref.encode()) <= 128)
        match = re.fullmatch(r"v1:([0-9a-f-]{36}):([01]):([1-9][0-9]{0,15}):([0-9a-f]{64})", row.proof_ref)
        _require(match is not None)
        join_id, created_text, epoch_text, _digest_text = match.groups()
        _uuid(join_id)
        created, epoch = created_text == "1", int(epoch_text)
        _require(epoch <= MAX_LIFECYCLE_EPOCH and epoch == data["lifecycle_epoch"] + int(created))
        _require(created == (data["join_id_at_issue"] is None))
        _require(scope.join is not None and scope.join.id == join_id and scope.lifecycle.epoch == epoch)
        role = scope.join.role.value if type(scope.join.role) is TenantAccountRole else scope.join.role
        _require(role == (scope.issuance.role if created else data["observed_join_role"]))
        if created:
            # Historical completion does not depend on a later feature-flag change.
            _require(role in {r.value for r in TenantAccountRole} and role != TenantAccountRole.OWNER)
        else:
            _require(join_id == data["join_id_at_issue"])
        _require(
            scope.issuance.state == "consumed"
            and scope.issuance.consumption_receipt_json == data["expected_receipt_json"]
            and type(scope.issuance.consumed_at) is datetime
        )
        _require(
            type(row.terminated_at) is datetime
            and row.terminated_at.tzinfo is None
            and row.created_at <= row.terminated_at < row.created_at + _RECONCILE_WINDOW
            and row.terminated_at == row.updated_at
            and row.terminated_at <= _now()
            and row.created_at <= scope.issuance.consumed_at <= row.terminated_at
        )
        _require(
            row.proof_ref
            == _proof(snapshot, data, join_id=join_id, join_role=role, created=created, result_epoch=epoch)
        )
        return (
            InvitationFinalizationFacts(snapshot, scope.facts, True, row.proof_ref, join_id, created, epoch),
            row,
            scope,
        )

    def inspect(self, attempt):
        """Reconstruct pending or completed SQL facts under the entire owner chain."""
        return self._inspect(attempt)[0]

    def finalize(self, attempt, *, expected: InvitationFinalizationFacts, receipt_json: str):
        """Internal orchestration handoff after exact P1 evidence; no network here.

        The DTO and receipt are consistency inputs, not authentication or P1
        capabilities. Only the trusted service may supply this handoff.
        """
        current, row, scope = self._inspect(attempt)
        _require(type(expected) is InvitationFinalizationFacts)
        _require(current.snapshot == expected.snapshot and current.issuance == expected.issuance)
        data = _strict_json(current.snapshot.desired_json, _SNAPSHOT_FIELDS)
        _require(type(receipt_json) is str and receipt_json == data["expected_receipt_json"])
        if current.completed:
            return current
        _require(not expected.completed)
        InvitationAuthorityRepository().record_consumption(self._session, receipt_json=receipt_json)
        created = scope.join is None
        if created:
            role = _new_role(scope.issuance.role)
            result = TenantService.persist_tenant_member(scope.workspace, scope.account, self._session, role)
            _require(result.membership_created is True)
            join_id = result.join.id
        else:
            # The shared helper intentionally updates existing roles; never call it here.
            join_id = scope.join.id
        self._session.flush()
        joins = list(
            self._session.scalars(
                sa.select(TenantAccountJoin)
                .where(
                    TenantAccountJoin.account_id == str(attempt.account_id),
                    TenantAccountJoin.tenant_id == str(attempt.workspace_id),
                )
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
        )
        _require(len(joins) == 1 and joins[0].id == join_id)
        join = joins[0]
        actual_role = join.role.value if type(join.role) is TenantAccountRole else join.role
        _require(actual_role == (scope.issuance.role if created else data["observed_join_role"]))
        lifecycle = self._one(Lifecycle, Lifecycle.lifecycle_id == data["lifecycle_id"])
        result_epoch = data["lifecycle_epoch"] + int(created)
        _require(lifecycle is not None and lifecycle.state == "active" and lifecycle.epoch == result_epoch)
        completed_at = _now()
        _require(row.created_at <= completed_at < row.created_at + _RECONCILE_WINDOW)
        row.operation_state = CasdoorOperationState.APPLIED
        row.termination_state = CasdoorTerminationState.CONFIRMED
        row.terminated_at = row.updated_at = completed_at
        row.termination_proof_kind = _PROOF_KIND
        row.proof_ref = _proof(
            current.snapshot, data, join_id=join_id, join_role=actual_role, created=created, result_epoch=result_epoch
        )
        self._session.flush([row])
        # Revalidate all rows, including owner/helper side effects, before root commit.
        completed = self.inspect(attempt)
        _require(completed.completed and completed.snapshot == expected.snapshot)
        return completed
