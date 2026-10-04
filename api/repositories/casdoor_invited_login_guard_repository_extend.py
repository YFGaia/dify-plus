"""One root-bound P3L intent exemption and exact invited LOCAL write readback.

Only this owner obtains P3M-B facts and invokes B3. Public completion/result
dataclasses cannot issue a guard or stand in for its recorded real B3 return.
This process-local registry is not admission, session or remote-effect authority.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session, SessionTransactionOrigin

from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MEMBERSHIP_FIELDS,
    RECEIPT_KIND,
    InvitedLocalWriteReceipt,
    postwrite_sha256,
)
from core.casdoor.leases import CasdoorLeases
from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.mapping import MappingIdentityContext, resolve_workspace_plan
from core.casdoor.ownership import OwnershipDecision
from models.account import Account, AccountStatus, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invitation_finalization_repository_extend import _proof, local_policy
from repositories.casdoor_invitation_operation_repository_extend import _SNAPSHOT_FIELDS, _strict_json
from repositories.casdoor_invited_login_scope_repository_extend import CasdoorInvitedLoginScopeRepository
from repositories.casdoor_login_scope_repository_extend import (
    MAX_SCOPE_ROWS,
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)
from services.casdoor_local_membership_service_extend import (
    CasdoorLocalMembershipService,
    LocalMembershipPersistence,
    RequiredIntentBarrier,
)
from services.casdoor_login_account_service_extend import _check_deadline

# Applies only to these new bounded snapshots, never to historical membership TEXT.
_MAX_TEXT_BYTES = 256 * 1024


class _InvitationIntentGuard:
    __slots__ = ()


@dataclass(slots=True, repr=False)
class _Binding:
    owner: object
    session: Session
    root: object
    observed: object
    plan: object
    roles: object
    leases: CasdoorLeases
    deadline: float
    rows: dict
    phase: str = "issued"
    result: object = None
    verified_scope: object = None
    verified_rows: object = None
    receipt: object = None
    audit_row: object = None
    receipt_append_started: bool = False


_REGISTRY: dict[_InvitationIntentGuard, _Binding] = {}


def _require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


def _binding(guard, session):
    _require(type(guard) is _InvitationIntentGuard)
    value = _REGISTRY.get(guard)
    _require(value is not None and value.session is session)
    root = session.get_transaction()
    _require(
        root is value.root
        and root is not None
        and root.is_active
        and root.origin is SessionTransactionOrigin.BEGIN
        and session.is_active
        and not session.in_nested_transaction()
        and not session.new
        and not session.dirty
        and not session.deleted
    )
    return value


def _invitation_exclusion(guard, session, account_id, workspace_id):
    """Direct use outside the exact invitation account/workspace always rejects."""
    value = _binding(guard, session)
    attempt = value.observed.attempt
    _require(
        type(account_id) is UUID
        and type(workspace_id) is UUID
        and account_id == attempt.account_id
        and workspace_id == attempt.workspace_id
        and value.phase in ("issued", "writing")
    )
    value.owner._invitation_unchanged(value)
    value.owner._identity_generation(value, increment=int(value.phase == "writing"))
    return str(value.observed.completion.snapshot.operation_id)


def _begin_invited_b3(guard, session, plan, fence, generation, withdrawals):
    value = _binding(guard, session)
    attempt = value.observed.attempt
    _require(
        value.phase == "issued"
        and plan is value.plan
        and type(fence) is int
        and fence == attempt.context.fence_epoch
        and type(generation) is int
        and generation == attempt.expected_generation
        and type(withdrawals) is tuple
        and not withdrawals
    )
    value.owner._before_write(value)
    value.phase = "writing"


def _invited_target_guard(guard, session, workspace_id):
    value = _binding(guard, session)
    _require(value.phase == "writing" and workspace_id in {t.workspace_id for t in value.plan.targets})
    # No old/withdrawn/regrant history may reach the pinned membership inspector.
    # Later targets remain empty; only already-processed targets gain new rows.
    joins = sa.select(Join.id).where(
        Join.account_id == str(value.plan.context.account_id), Join.tenant_id == str(workspace_id)
    )
    history = session.scalar(
        sa.select(History.id)
        .where(
            sa.or_(
                sa.and_(
                    History.account_id == str(value.plan.context.account_id), History.workspace_id == str(workspace_id)
                ),
                History.join_id.in_(joins),
            )
        )
        .limit(1)
        .with_for_update()
    )
    _require(history is None)
    value.owner._invitation_unchanged(value)
    value.owner._identity_generation(value, increment=1)
    return guard if workspace_id == value.observed.attempt.workspace_id else None


def _authorize_receipt_append(guard, session, receipt):
    """A closed parser result or copied receipt is not a producer capability."""
    value = _binding(guard, session)
    _require(
        value.phase == "receipting"
        and receipt is value.receipt
        and type(receipt) is InvitedLocalWriteReceipt
        and not value.receipt_append_started
    )
    value.owner._recheck_verified(value)
    _require(value.owner._receipt_from_verified(value) == receipt)
    value.receipt_append_started = True


class CasdoorInvitedLoginGuardRepository:
    def __init__(self, session, configuration_factory):
        self.session = session
        self.configuration_factory = configuration_factory

    def _rows(self, model, *conditions, cap=MAX_SCOPE_ROWS):
        """Check bounded SQL byte lengths before loading any TEXT in this owner."""
        table = model.__table__
        key = tuple(table.primary_key.columns)
        text_columns = tuple(
            c
            for c in table.columns
            if isinstance(c.type, sa.Text) or isinstance(getattr(c.type, "impl", None), sa.Text)
        )
        lengths = tuple(
            sa.func.length(sa.cast(c, sa.LargeBinary))
            if self.session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(c)
            for c in text_columns
        )
        headers = tuple(
            self.session.execute(sa.select(*key, *lengths).where(*conditions).order_by(*key).limit(cap + 1))
        )
        _require(len(headers) <= cap)
        for row in headers:
            for column, length in zip(text_columns, tuple(row)[len(key) :], strict=True):
                _require(
                    (length is None and column.nullable) or (type(length) is int and 0 <= length <= _MAX_TEXT_BYTES)
                )
        bounds = [sa.or_(column.is_(None), length <= _MAX_TEXT_BYTES) for column, length in zip(text_columns, lengths)]
        rows = tuple(
            self.session.execute(sa.select(*table.columns).where(*conditions, *bounds).order_by(*key).limit(cap + 1))
        )
        _require(
            len(rows) == len(headers)
            and [tuple(getattr(row, c.name) for c in key) for row in rows]
            == [tuple(row)[: len(key)] for row in headers]
        )
        return rows

    def _one(self, model, *conditions):
        rows = self._rows(model, *conditions, cap=1)
        _require(len(rows) == 1)
        return rows[0]

    def _scope(self, value):
        attempt = value.observed.attempt
        return CasdoorLoginScopeRepository(self.session, self.configuration_factory)._project_scope(
            attempt.context,
            attempt.key,
            attempt.account_id,
            creation_email=None,
            extra_workspace_ids=(attempt.workspace_id,),
        )

    def _snapshot_rows(self, observed):
        attempt, facts, scope = observed.attempt, observed.completion, observed.scope
        data = _strict_json(facts.snapshot.desired_json, _SNAPSHOT_FIELDS)
        return {
            "operation": self._one(Intent, Intent.id == str(facts.snapshot.operation_id)),
            "issuance": self._one(Issuance, Issuance.issuance_id == str(attempt.issuance_id)),
            "lifecycle": self._one(Lifecycle, Lifecycle.lifecycle_id == data["lifecycle_id"]),
            "integration": self._one(Integration, Integration.id == str(attempt.context.integration_id)),
            "revision": self._one(Revision, Revision.id == str(attempt.context.revision_id)),
            "namespaces": self._rows(Namespace, Namespace.integration_id == str(attempt.context.integration_id)),
            "account": self._one(Account, Account.id == str(attempt.account_id)),
            "identities": self._rows(Identity, Identity.account_id == str(attempt.account_id)),
            "workspaces": self._rows(Tenant, Tenant.id.in_([row.id for row in scope.workspaces])),
            "joins": self._rows(Join, Join.account_id == str(attempt.account_id)),
        }

    def _leases(self, value):
        local_policy()
        _require(
            type(value.leases) is CasdoorLeases
            and value.leases._deadline == value.deadline
            and value.leases.canonical_keys == value.observed.scope.lease_scope.canonical_keys
        )
        _check_deadline(value.deadline)
        value.leases.ensure_owned()
        _check_deadline(value.deadline)

    def _invitation_unchanged(self, value):
        facts, attempt = value.observed.completion, value.observed.attempt
        data = _strict_json(facts.snapshot.desired_json, _SNAPSHOT_FIELDS)
        _require(self._one(Intent, Intent.id == str(facts.snapshot.operation_id)) == value.rows["operation"])
        _require(self._one(Issuance, Issuance.issuance_id == str(attempt.issuance_id)) == value.rows["issuance"])
        _require(self._one(Lifecycle, Lifecycle.lifecycle_id == data["lifecycle_id"]) == value.rows["lifecycle"])
        captured_join = next(row for row in value.rows["joins"] if row.id == facts.join_id)
        _require(self._one(Join, Join.id == facts.join_id) == captured_join)
        # Always use captured role/result epoch; never synthesize proof from drift.
        _require(
            facts.proof_ref
            == _proof(
                facts.snapshot,
                data,
                join_id=facts.join_id,
                join_role=captured_join.role.value,
                created=facts.membership_created,
                result_epoch=facts.result_epoch,
            )
            and value.rows["operation"].proof_ref == facts.proof_ref
            and value.rows["operation"].desired_json == facts.snapshot.desired_json
            and value.rows["issuance"].payload_json == facts.issuance.payload_json
        )

    def _identity_generation(self, value, *, increment):
        identity = self.session.execute(
            sa.select(Identity.sync_generation, Identity.namespace_id, Identity.account_id).where(
                Identity.id == str(value.plan.context.identity_id)
            )
        ).one_or_none()
        _require(
            identity is not None
            and tuple(identity)
            == (
                value.observed.attempt.expected_generation + increment,
                str(value.plan.context.namespace_id),
                str(value.plan.context.account_id),
            )
        )

    def prelock(self, attempt, *, roles, leases, deadline):
        """Obtain actual P3M-B facts here; never accept completion or scope DTOs."""
        local_policy()
        _check_deadline(deadline)
        observed = CasdoorInvitedLoginScopeRepository(
            self.session, self.configuration_factory
        ).prelock_and_recheck_completed_invitation(attempt)
        before = observed.scope
        _require(
            before.histories == ()
            and before.account is not None
            and before.account.status is AccountStatus.ACTIVE
            and before.account.initialized_at is not None
            and len(before.intents) == 1
            and before.intents[0].id == str(observed.completion.snapshot.operation_id)
        )
        own = next(row for row in before.identities if row.namespace_id == str(attempt.context.namespace_id))
        c = attempt.context
        context = MappingIdentityContext(
            c.integration_id,
            c.revision_id,
            c.namespace_id,
            UUID(own.id),
            attempt.account_id,
            c.config_digest,
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.subject,
        )
        plan = resolve_workspace_plan(
            configuration=before.configuration, snapshot=roles, context=context, availability=before.availability()
        )
        value = _Binding(
            self,
            self.session,
            self.session.get_transaction(),
            observed,
            plan,
            roles,
            leases,
            deadline,
            self._snapshot_rows(observed),
        )
        self._before_write(value)
        guard = _InvitationIntentGuard()
        _REGISTRY[guard] = value
        return guard

    def _before_write(self, value):
        self._leases(value)
        _require(self._scope(value) == value.observed.scope)
        _require(self._snapshot_rows(value.observed) == value.rows)
        self._invitation_unchanged(value)
        self._identity_generation(value, increment=0)
        self._leases(value)

    def persist_once(self, guard):
        """Save the actual B3 result internally; callers cannot supply a result."""
        value = _binding(guard, self.session)
        _require(value.owner is self and value.phase == "issued")
        result = CasdoorLocalMembershipService(self.session).persist_local_memberships(
            value.plan,
            expected_fence_epoch=value.observed.attempt.context.fence_epoch,
            expected_generation=value.observed.attempt.expected_generation,
            invitation_guard=guard,
        )
        _require(value.phase == "writing")
        value.result = result
        value.phase = "written"
        return result

    def verify_after(self, guard):
        """Read G+1 directly; P3L's generation-G inspector must not run again."""
        value = _binding(guard, self.session)
        _require(value.owner is self and value.phase == "written")
        result, before, plan = value.result, value.observed.scope, value.plan
        c = plan.context
        _require(
            type(result) is LocalMembershipPersistence
            and (
                result.account_id,
                result.identity_id,
                result.namespace_id,
                result.revision_id,
                result.fence_epoch,
                result.generation,
            )
            == (
                c.account_id,
                c.identity_id,
                c.namespace_id,
                c.revision_id,
                value.observed.attempt.context.fence_epoch,
                value.observed.attempt.expected_generation + 1,
            )
        )
        _require(not result.withdrawals and not result._controlled_effects)
        _require(len(result.workspaces) == len(plan.targets))
        self._invitation_unchanged(value)
        self._identity_generation(value, increment=1)
        current = self._scope(value)
        _require(
            current.configuration == before.configuration
            and current.namespaces == before.namespaces
            and current.account == before.account
            and current.workspaces == before.workspaces
            and current.intents == before.intents
            and current.lease_scope.canonical_keys == before.lease_scope.canonical_keys
        )
        actual = self._snapshot_rows(value.observed)
        for key in (
            "operation",
            "issuance",
            "lifecycle",
            "integration",
            "revision",
            "namespaces",
            "account",
            "workspaces",
        ):
            _require(actual[key] == value.rows[key])
        old_ids = {row.id: row for row in value.rows["identities"]}
        new_ids = {row.id: row for row in actual["identities"]}
        prior, own = old_ids.pop(str(c.identity_id)), new_ids.pop(str(c.identity_id), None)
        _require(own is not None and old_ids == new_ids)
        _require(
            all(
                getattr(own, key) == item
                for key, item in prior._mapping.items()
                if key not in ("sync_generation", "updated_at")
            )
        )
        _require(own.sync_generation == prior.sync_generation + 1)
        _require(
            type(own.updated_at) is datetime
            and type(prior.updated_at) is datetime
            and own.updated_at.utcoffset() in (None, timedelta(0))
            and prior.updated_at.utcoffset() in (None, timedelta(0))
            and own.updated_at >= prior.updated_at
        )
        all_joins = {row.id: row for row in actual["joins"]}
        for row in value.rows["joins"]:
            _require(all_joins.pop(row.id, None) == row)
        created = set()
        for target, summary in zip(plan.targets, result.workspaces, strict=True):
            _require(
                target.workspace_id == summary.workspace_id and summary.intent_barrier is RequiredIntentBarrier.CLEAR
            )
            _require(not summary.membership_regranted)
            if summary.membership_created:
                _require(summary.outcome in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP))
                _require(summary.ownership_decision is OwnershipDecision.NEW_JOIN_REQUIRED)
                created.add(str(summary.join_id))
            else:
                _require(
                    summary.outcome is LocalRoleOutcome.PRESERVED
                    and summary.membership_id is None
                    and summary.ownership_decision
                    in (OwnershipDecision.PRESERVE_UNMANAGED, OwnershipDecision.OWNER_PROTECTED)
                    and not summary.role_changed
                    and not summary.metadata_changed
                    and summary.finalization is None
                )
        _require(set(all_joins) == created)
        _require(all(str(summary.join_id) in {row.id for row in actual["joins"]} for summary in result.workspaces))
        CasdoorLoginScopeRepository(self.session, self.configuration_factory)._verify_memberships(
            before, current, plan, result
        )
        self._leases(value)
        # Configuration is reconstructed once more after the lease I/O boundary.
        _require(self._scope(value) == current)
        self._invitation_unchanged(value)
        self._leases(value)
        # The last lease check is also an I/O boundary. Re-read the complete
        # scalar scope and bounded full rows after it before returning to commit.
        _require(self._scope(value) == current)
        _require(self._snapshot_rows(value.observed) == actual)
        _check_deadline(value.deadline)
        value.verified_scope = current
        value.verified_rows = actual
        value.phase = "verified"
        return result

    @staticmethod
    def _privacy_row(row, fields):
        return {
            field: getattr(row, field).value if isinstance(getattr(row, field), StrEnum) else getattr(row, field)
            for field in fields
        }

    def _postwrite_digest(self, value, scope):
        identity = next(row for row in scope.identities if row.id == str(value.plan.context.identity_id))
        return postwrite_sha256(
            str(value.observed.completion.snapshot.operation_id),
            identity=self._privacy_row(identity, IDENTITY_FIELDS),
            joins=tuple(self._privacy_row(row, JOIN_FIELDS) for row in scope.joins),
            memberships=tuple(self._privacy_row(row, MEMBERSHIP_FIELDS) for row in scope.histories),
        )

    def _receipt_from_verified(self, value):
        facts, attempt, result = value.observed.completion, value.observed.attempt, value.result
        context = value.plan.context
        invited = tuple(row for row in value.verified_scope.joins if row.tenant_id == str(attempt.workspace_id))
        _require(len(invited) == 1 and invited[0].id == facts.join_id)
        # Exactly the actual B3 summaries, in their existing order. A P3L-only
        # workspace is bound separately; it gets no invented barrier observation.
        results = [
            {
                "workspace_id": str(item.workspace_id),
                "ownership_decision": item.ownership_decision.value,
                "intent_barrier": item.intent_barrier.value,
                "outcome": item.outcome.value,
                "join_id": str(item.join_id) if item.join_id is not None else None,
                "membership_id": str(item.membership_id) if item.membership_id is not None else None,
                "current_role": item.current_role.value if item.current_role is not None else None,
                "membership_created": item.membership_created,
                "role_changed": item.role_changed,
                "metadata_changed": item.metadata_changed,
                "membership_regranted": item.membership_regranted,
            }
            for item in result.workspaces
        ]
        return InvitedLocalWriteReceipt.from_values(
            {
                "schema_version": 1,
                "receipt_kind": RECEIPT_KIND,
                "references": {
                    "operation_id": str(facts.snapshot.operation_id),
                    "issuance_id": str(attempt.issuance_id),
                    "integration_id": str(context.integration_id),
                    "namespace_id": str(context.namespace_id),
                    "revision_id": str(context.revision_id),
                    "identity_id": str(context.identity_id),
                    "account_id": str(context.account_id),
                    "workspace_id": str(attempt.workspace_id),
                    "invitation_join_id": facts.join_id,
                },
                "generation_before": attempt.expected_generation,
                "generation_after": result.generation,
                "fence_epoch": result.fence_epoch,
                "scope_digest": facts.snapshot.scope_digest,
                "completion_proof_ref": facts.proof_ref,
                "payload_digest": value.rows["issuance"].payload_digest,
                "results": results,
                "postwrite_sha256": self._postwrite_digest(value, value.verified_scope),
            }
        )

    def _recheck_verified(self, value):
        _require(value.verified_scope is not None and value.verified_rows is not None)
        current = self._scope(value)
        _require(current == value.verified_scope)
        _require(self._snapshot_rows(value.observed) == value.verified_rows)
        self._invitation_unchanged(value)
        self._identity_generation(value, increment=1)
        CasdoorLoginScopeRepository(self.session, self.configuration_factory)._verify_memberships(
            value.observed.scope, current, value.plan, value.result
        )
        if value.receipt is not None:
            _require(self._postwrite_digest(value, current) == value.receipt.values()["postwrite_sha256"])
        _check_deadline(value.deadline)

    def append_receipt(self, guard):
        """Append only from this owner's already-verified real P3L/B3 write."""
        value = _binding(guard, self.session)
        _require(value.owner is self and value.phase == "verified")
        self._recheck_verified(value)
        self._leases(value)
        self._recheck_verified(value)
        value.receipt = self._receipt_from_verified(value)
        value.phase = "receipting"
        audit = CasdoorAuditRepository(self.session)
        value.audit_row = audit._append_invited_write_receipt(value.receipt, invitation_guard=guard)
        self._recheck_verified(value)
        audit._read_invited_write_receipt(value.receipt, dict(value.audit_row._mapping))
        self._leases(value)
        # No lease I/O follows these complete final SQL/projection checks.
        self._recheck_verified(value)
        audit._read_invited_write_receipt(value.receipt, dict(value.audit_row._mapping))
        _require(self._receipt_from_verified(value) == value.receipt)
        _check_deadline(value.deadline)
        value.phase = "receipted"

    @staticmethod
    def revoke(guard):
        if type(guard) is _InvitationIntentGuard:
            _REGISTRY.pop(guard, None)
