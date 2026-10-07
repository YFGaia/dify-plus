"""Bounded LOCAL login scope reads in the actual private C1 transaction.

Discovery is not authentication or locking missing rows. The caller leases the
entire discovered set; any changed set requires whole rollback and fresh outer
coordination. Discovery projects historical scalars without history TEXT.
Relevant current/controlled full rows use only the original bounded owner.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field as dataclass_field
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction, AdmissionContext
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.leases import CasdoorLeaseScope, WorkspaceMemberScope
from core.casdoor.local_roles import LOCAL_ROLES, LocalRoleOutcome
from core.casdoor.mapping import (
    BuiltinResolution,
    DesiredWorkspacePlan,
    ServerWorkspaceAvailability,
    WorkspaceAvailability,
    WorkspaceState,
)
from core.casdoor.ownership import (
    MembershipBackend,
    OwnershipDecision,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountRole,
    TenantStatus,
)
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceLifecycle,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from services.account_email import normalize_email
from services.casdoor_local_membership_service_extend import LocalMembershipPersistence
from services.casdoor_login_account_service_extend import (
    _check_consistency,
    _complete,
    _PreparedLoginAccount,
)
from sqlalchemy.orm import Session

from repositories.casdoor_account_preflight_repository_extend import (
    CasdoorAccountPreflightRepository,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from repositories.casdoor_generation_repository_extend import (
    MAX_GENERATION,
    GenerationPlanVersion,
)
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_membership_repository_extend import (
    CasdoorMembershipRepository,
    _LocalMembershipEffect,
)
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentRepository,
)

MAX_SCOPE_ROWS = 2048
MAX_NAMESPACE_ROWS = 2000


class CasdoorLoginScopeConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


def _uuid(value) -> UUID:
    try:
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (TypeError, ValueError, AttributeError):
        raise CasdoorLoginScopeConflict() from None


def _counter(value) -> None:
    if type(value) is not int or not 0 <= value <= MAX_GENERATION:
        raise CasdoorLoginScopeConflict()


@dataclass(frozen=True, repr=False)
class LoginScope:
    """Consistency observation, not an authenticated capability or NEW receipt."""

    configuration: CasdoorConfiguration
    namespaces: tuple
    account: tuple | None
    identities: tuple
    workspaces: tuple
    joins: tuple
    histories: tuple
    intents: tuple
    lease_scope: CasdoorLeaseScope
    _archived_facts: tuple[str, ...] = dataclass_field(default=(), repr=False)

    def availability(self) -> ServerWorkspaceAvailability:
        configured = {self.configuration.default_workspace_id} | {
            item.workspace_id for item in self.configuration.workspace_mappings
        }
        return ServerWorkspaceAvailability(
            tuple(
                WorkspaceAvailability(
                    _uuid(row.id),
                    WorkspaceState.NORMAL,
                    tuple(BuiltinResolution(role.value, role.value) for role in LOCAL_ROLES),
                )
                for row in self.workspaces
                if _uuid(row.id) in configured
            )
        )


class CasdoorLoginScopeRepository:
    def __init__(self, session: Session, configuration_factory: Callable[[Session], CasdoorConfigurationRepository]):
        self.session = session
        self.configuration_factory = configuration_factory

    def _clean(self):
        s = self.session
        if (
            not isinstance(s, Session)
            or not s.is_active
            or not s.in_transaction()
            or s.in_nested_transaction()
            or s.new
            or s.dirty
            or s.deleted
        ):
            raise CasdoorLoginScopeConflict()

    def _rows(self, statement, *, lock=False, cap=MAX_SCOPE_ROWS):
        if lock:
            statement = statement.with_for_update()
        rows = tuple(self.session.execute(statement.limit(cap + 1)))
        if len(rows) > cap:
            raise CasdoorLoginScopeConflict()
        return rows

    def _configuration(self, context: AdmissionContext, *, lock):
        c = context
        row = self.session.execute(
            sa.select(Integration.id, Integration.enabled, Integration.active_revision_id)
            .where(Integration.slot == 1)
            .with_for_update()
            if lock
            else sa.select(Integration.id, Integration.enabled, Integration.active_revision_id).where(
                Integration.slot == 1
            )
        ).one_or_none()
        if row is None or tuple(row) != (str(c.integration_id), True, str(c.revision_id)):
            raise CasdoorLoginScopeConflict()
        ns = self._rows(
            sa.select(
                Namespace.id,
                Namespace.integration_id,
                Namespace.expected_issuer,
                Namespace.organization,
                Namespace.application,
                Namespace.client_id,
                Namespace.lifecycle,
                Namespace.fence_epoch,
            )
            .where(Namespace.integration_id == str(c.integration_id))
            .order_by(Namespace.id),
            lock=lock,
            cap=MAX_NAMESPACE_ROWS,
        )
        for item in ns:
            _uuid(item.id)
            _counter(item.fence_epoch)
            if item.lifecycle not in tuple(CasdoorNamespaceLifecycle):
                raise CasdoorLoginScopeConflict()
        active = next((item for item in ns if item.id == str(c.namespace_id)), None)
        if active is None or tuple(active)[1:] != (
            str(c.integration_id),
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            CasdoorNamespaceLifecycle.ACTIVE,
            c.fence_epoch,
        ):
            raise CasdoorLoginScopeConflict()
        # Reuse original reconstruction/digest/core validation with fresh ORM state.
        # No secret is decrypted and no new parser or activation proof is invented.
        self.session.expire_all()
        owner = self.configuration_factory(self.session)
        if type(owner) is not CasdoorConfigurationRepository or owner.session is not self.session:
            raise CasdoorLoginScopeConflict()
        revision = owner._revision(str(c.integration_id), str(c.revision_id))
        if revision.namespace_id != str(c.namespace_id) or revision.config_digest != c.config_digest:
            raise CasdoorLoginScopeConflict()
        return owner._configuration(revision), ns

    def discover(
        self, prepared: _PreparedLoginAccount, *, _lock=False, _parents=None, _ordinary_guard=None
    ) -> LoginScope:
        """Full bounded scalar read; unlocked discovery owns no write/lease lifecycle.

        _parents is the already leased workspace set. A locked read must lock
        that set before joins/history/intents and reject expansion, never acquire
        a newly discovered parent late in the hierarchy.
        """
        self._clean()
        if not _complete(prepared, _PreparedLoginAccount) or type(prepared.account_id) is not UUID:
            raise CasdoorLoginScopeConflict()
        _check_consistency(prepared.plan, prepared.preflight)
        return self._project_scope(
            prepared.plan.context,
            prepared.key,
            prepared.account_id,
            creation_email=prepared.plan.creation_email,
            extra_workspace_ids=(),
            _lock=_lock,
            _parents=_parents,
            _ordinary_guard=_ordinary_guard,
        )

    def _project_scope(
        self,
        context: AdmissionContext,
        key: VerifiedIdentityKey,
        account_id: UUID,
        *,
        creation_email: str | None,
        extra_workspace_ids: tuple[UUID, ...],
        _lock=False,
        _parents=None,
        _ordinary_guard=None,
    ) -> LoginScope:
        """Shared scalar projection; extra parents grant no mapping or admission.

        Invited discovery uses the non-locking default. Only the unchanged
        ordinary caller uses its existing leased-parent locking lifecycle.
        """
        self._clean()
        if (
            type(context) is not AdmissionContext
            or type(key) is not VerifiedIdentityKey
            or type(account_id) is not UUID
            or (creation_email is not None and type(creation_email) is not str)
            or type(extra_workspace_ids) is not tuple
            or any(type(workspace_id) is not UUID for workspace_id in extra_workspace_ids)
        ):
            raise CasdoorLoginScopeConflict()
        configuration, namespaces = self._configuration(context, lock=_lock)
        configured = {configuration.default_workspace_id} | {m.workspace_id for m in configuration.workspace_mappings}
        c, aid = context, str(account_id)
        accounts = self._rows(
            sa.select(
                Account.id,
                Account.email,
                Account.normalized_email,
                Account.status,
                Account.initialized_at,
                Account.name,
                Account.avatar,
                Account.interface_language,
                Account.timezone,
                Account.interface_theme,
                Account.password.is_(None).label("password_unset"),
                Account.password_salt.is_(None).label("password_salt_unset"),
            )
            .where(Account.id == aid)
            .order_by(Account.id),
            lock=_lock,
        )
        account = accounts[0] if accounts else None
        identities = self._rows(
            sa.select(
                Identity.id,
                Identity.namespace_id,
                Identity.account_id,
                Identity.issuer,
                Identity.organization,
                Identity.subject,
                Identity.subject_digest,
                Identity.sync_generation,
            )
            .where(
                sa.or_(
                    Identity.account_id == aid,
                    sa.and_(
                        Identity.namespace_id == str(c.namespace_id),
                        Identity.subject_digest == key.subject_digest,
                    ),
                )
            )
            .order_by(Identity.namespace_id, Identity.id),
            lock=_lock,
        )
        ns_ids = {row.id for row in namespaces}
        ns_by_id = {row.id: row for row in namespaces}
        for row in identities:
            for field in ("id", "namespace_id", "account_id"):
                _uuid(getattr(row, field))
            _counter(row.sync_generation)
            if row.account_id != aid or row.namespace_id not in ns_ids:
                raise CasdoorLoginScopeConflict()
            ns = ns_by_id[row.namespace_id]
            identity_key = VerifiedIdentityKey(_uuid(row.namespace_id), row.issuer, row.organization, row.subject)
            if (row.issuer, row.organization, row.subject_digest) != (
                ns.expected_issuer,
                ns.organization,
                identity_key.subject_digest,
            ):
                raise CasdoorLoginScopeConflict()
        workspaces = None
        if _lock:
            if _parents is None:
                raise CasdoorLoginScopeConflict()
            workspaces = self._workspaces(_parents, configured=configured, lock=True)
            if _ordinary_guard is not None:
                from repositories.casdoor_terminal_local_invitation_repository_extend import (
                    _lock_ordinary_terminal_parents,
                )

                _lock_ordinary_terminal_parents(_ordinary_guard, self.session, context, key, account_id, workspaces)
        archive_facts = ()
        selected_identity = [r for r in identities if r.namespace_id == str(c.namespace_id)]
        if len(identities) > 1:
            if len(selected_identity) != 1:
                raise CasdoorLoginScopeConflict()
            from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

            factual = CasdoorLocalLifecycleRepository(self.session)
            archive = factual._archived_release_facts(
                account_id=aid, namespace_id=c.namespace_id, identity_id=selected_identity[0].id
            )
            if _lock and not {UUID(w) for w in archive[2]} <= set(_parents):
                raise CasdoorLoginScopeConflict()
            archive_facts = archive[0]
            current_rows = factual._archived_rows(
                History, sa.and_(History.account_id == aid, History.namespace_id == str(c.namespace_id))
            )
            for row in current_rows:
                if row["source"] is CasdoorMembershipSource.ADOPT:
                    _membership_id, facts = factual._adopted_current_refs(
                        account_id=aid,
                        namespace_id=c.namespace_id,
                        identity_id=selected_identity[0].id,
                        workspace_id=row["workspace_id"],
                    )
                    archive_facts += facts[len(archive[0]) :]
        joins = self._rows(
            sa.select(Join.id, Join.account_id, Join.tenant_id, Join.role, Join.invited_by)
            .where(Join.account_id == aid)
            .order_by(Join.tenant_id, Join.id),
            lock=_lock,
        )
        for row in joins:
            for field in ("id", "account_id", "tenant_id"):
                _uuid(getattr(row, field))
            if row.invited_by is not None:
                _uuid(row.invited_by)
        join_ids = sa.select(Join.id).where(Join.account_id == aid)
        histories = self._rows(
            sa.select(
                History.id,
                History.namespace_id,
                History.identity_id,
                History.account_id,
                History.workspace_id,
                History.join_id,
                History.ownership,
                History.ownership_epoch,
                History.source,
                History.desired_generation,
                History.revision_id,
                History.last_applied_fingerprint,
                History.finalization,
                History.tombstone,
            )
            .where(sa.or_(History.account_id == aid, History.join_id.in_(join_ids)))
            .order_by(History.namespace_id, History.workspace_id, History.id),
            lock=_lock,
        )
        self._history_associations(histories, namespaces, identities, joins, aid)
        history_ids = sa.select(History.id).where(sa.or_(History.account_id == aid, History.join_id.in_(join_ids)))
        intents = self._rows(
            sa.select(
                Intent.id,
                Intent.namespace_id,
                Intent.identity_id,
                Intent.account_id,
                Intent.workspace_id,
                Intent.membership_id,
                Intent.revision_id,
                Intent.kind,
                Intent.generation,
                Intent.ownership_epoch,
                Intent.fence_epoch,
                Intent.operation_state,
                Intent.termination_state,
            )
            .where(
                Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                sa.or_(Intent.account_id == aid, Intent.membership_id.in_(history_ids)),
            )
            .order_by(Intent.namespace_id, Intent.id),
            lock=_lock,
        )
        # Pull every linked history even for a foreign/malformed account reference;
        # it must be rejected or accounted for, never disappear via an inner join.
        linked = (
            self._rows(
                sa.select(History.id, History.account_id, History.workspace_id, History.namespace_id, History.identity_id)
                .where(History.id.in_([row.membership_id for row in intents if row.membership_id is not None]))
                .order_by(History.id),
                lock=_lock,
            )
            if intents
            else ()
        )
        linked_by_id = {row.id: row for row in linked}
        for row in intents:
            for field in ("id", "namespace_id", "identity_id", "account_id", "revision_id"):
                _uuid(getattr(row, field))
            for field in ("generation", "ownership_epoch", "fence_epoch"):
                _counter(getattr(row, field))
            if row.workspace_id is not None:
                _uuid(row.workspace_id)
            if row.namespace_id not in ns_ids or row.account_id != aid:
                raise CasdoorLoginScopeConflict()
            if row.membership_id is not None:
                _uuid(row.membership_id)
                history = linked_by_id.get(row.membership_id)
                if (
                    history is None
                    or (history.account_id, history.namespace_id, history.identity_id)
                    != (aid, row.namespace_id, row.identity_id)
                    or row.workspace_id not in (None, history.workspace_id)
                ):
                    raise CasdoorLoginScopeConflict()
        wanted = {configuration.default_workspace_id} | {m.workspace_id for m in configuration.workspace_mappings}
        wanted.update(extra_workspace_ids)
        wanted.update(_uuid(row.tenant_id) for row in joins)
        wanted.update(_uuid(row.workspace_id) for row in histories)
        wanted.update(_uuid(row.workspace_id) for row in intents if row.workspace_id is not None)
        wanted.update(_uuid(row.workspace_id) for row in linked)
        if len(wanted) > MAX_SCOPE_ROWS:
            raise CasdoorLoginScopeConflict()
        if _lock:
            if wanted != set(_parents):
                raise CasdoorLoginScopeConflict()
        else:
            workspaces = self._workspaces(wanted, configured=configured, lock=False)
        emails = set()
        if account is not None:
            emails.add(account.email)
            if account.normalized_email is not None:
                emails.add(account.normalized_email)
        if creation_email is not None:
            emails.add(creation_email)
        lease_scope = CasdoorLeaseScope(
            c.namespace_id,
            c.subject,
            tuple(sorted(emails)),
            (account_id,),
            tuple(WorkspaceMemberScope(w, account_id) for w in sorted(wanted, key=str)),
        )
        return LoginScope(
            configuration,
            namespaces,
            account,
            identities,
            workspaces,
            joins,
            histories,
            intents,
            lease_scope,
            archive_facts,
        )

    def _workspaces(self, ids, *, configured, lock):
        rows = self._rows(
            sa.select(Tenant.id, Tenant.status).where(Tenant.id.in_([str(v) for v in ids])).order_by(Tenant.id),
            lock=lock,
        )
        if {row.id for row in rows} != {str(v) for v in ids} or any(
            not isinstance(row.status, TenantStatus)
            or (_uuid(row.id) in configured and row.status is not TenantStatus.NORMAL)
            for row in rows
        ):
            raise CasdoorLoginScopeConflict()
        return rows

    def _history_associations(self, rows, namespaces, identities, joins, aid):
        ns_ids = {row.id for row in namespaces}
        # Historical identity references may legitimately survive unlink. Existing
        # references must still belong to this account/namespace; do not mistake
        # an identity belonging to another account for a deleted reference.
        referenced = (
            self._rows(
                sa.select(Identity.id, Identity.namespace_id, Identity.account_id)
                .where(Identity.id.in_({row.identity_id for row in rows}))
                .order_by(Identity.id)
            )
            if rows
            else ()
        )
        identities_by_id = {row.id: row for row in (*identities, *referenced)}
        referenced_joins = (
            self._rows(
                sa.select(Join.id, Join.account_id, Join.tenant_id)
                .where(Join.id.in_({row.join_id for row in rows if row.join_id is not None}))
                .order_by(Join.id)
            )
            if rows
            else ()
        )
        joins_by_id = {row.id: row for row in (*joins, *referenced_joins)}
        revisions = (
            self._rows(
                sa.select(Revision.id, Revision.integration_id, Revision.namespace_id)
                .where(Revision.id.in_({row.revision_id for row in rows}))
                .order_by(Revision.id)
            )
            if rows
            else ()
        )
        revisions_by_id = {row.id: row for row in revisions}
        for row in rows:
            for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "revision_id"):
                _uuid(getattr(row, field))
            if row.join_id is not None:
                _uuid(row.join_id)
            _counter(row.ownership_epoch)
            _counter(row.desired_generation)
            rev = revisions_by_id.get(row.revision_id)
            if (
                row.account_id != aid
                or row.namespace_id not in ns_ids
                or rev is None
                or rev.namespace_id != row.namespace_id
                or rev.integration_id != namespaces[0].integration_id
            ):
                raise CasdoorLoginScopeConflict()
            identity = identities_by_id.get(row.identity_id)
            if identity is not None and (identity.namespace_id, identity.account_id) != (row.namespace_id, aid):
                raise CasdoorLoginScopeConflict()
            join = joins_by_id.get(row.join_id)
            if join is not None and (join.account_id, join.tenant_id) != (aid, row.workspace_id):
                raise CasdoorLoginScopeConflict()

    def prelock_and_recheck(self, prepared, discovered: LoginScope, *, _ordinary_guard=None) -> LoginScope:
        current = self.discover(
            prepared,
            _lock=True,
            _parents=tuple(_uuid(row.id) for row in discovered.workspaces),
            _ordinary_guard=_ordinary_guard,
        )
        if current != discovered:
            raise CasdoorLoginScopeConflict()
        self._intent_barrier(prepared.account_id, current, _ordinary_guard=_ordinary_guard)
        return current

    def _intent_barrier(self, account_id, scope, *, _ordinary_guard=None):
        owner = CasdoorRequiredIntentRepository(self.session)
        for workspace in scope.workspaces:
            if owner.read_locked(account_id, _uuid(workspace.id), _ordinary_guard=_ordinary_guard):
                raise CasdoorLoginScopeConflict()
        excluded = None
        if _ordinary_guard is not None:
            from repositories.casdoor_terminal_local_invitation_repository_extend import _ordinary_terminal_exclusion

            excluded = _ordinary_terminal_exclusion(_ordinary_guard, self.session, account_id)
        if any(row.id != excluded for row in scope.intents):
            raise CasdoorLoginScopeConflict()

    def _classify_local_changes(self, before: LoginScope, plan: DesiredWorkspacePlan, *, _ordinary_guard=None):
        """Freeze relevant original bounded rows before B2 can change them."""
        targets = {str(target.workspace_id): target for target in plan.targets}
        c = plan.context
        identity = next((r for r in before.identities if r.id == str(c.identity_id)), None)
        owner = CasdoorMembershipRepository(self.session)
        withdrawals, rows = [], []
        for history in before.histories:
            target = targets.get(history.workspace_id)
            mapped_override = (
                history.ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
                and history.tombstone is True
                and target is not None
                and target.reason is CasdoorDecisionReason.ROLE_MAPPING
                and bool(target.matched_role_refs)
            )
            if not mapped_override and (
                history.ownership is not CasdoorMembershipOwnership.MANAGED or history.tombstone
            ):
                continue
            if history.namespace_id != str(c.namespace_id) or history.identity_id != str(c.identity_id):
                raise CasdoorLoginScopeConflict()
            if identity is None or identity.sync_generation < 1:
                raise CasdoorLoginScopeConflict()
            version = GenerationPlanVersion(
                plan,
                next(r.fence_epoch for r in before.namespaces if r.id == str(c.namespace_id)),
                identity.sync_generation,
            )
            workspace_id = _uuid(history.workspace_id)
            view = owner.inspect(version, workspace_id, backend=MembershipBackend.LOCAL)
            if view.decision is OwnershipDecision.MAPPED_REGRANT_REQUIRED:
                token = owner.prepare_local_regrant(version, target, _ordinary_guard=_ordinary_guard)
                rows.append(token.state[0])
            elif mapped_override:
                continue  # Preserve a retained/malformed/blocked manual override.
            elif target is None or view.decision is OwnershipDecision.CONTROLLED_WITHDRAWN:
                row, join, _ = owner._read_local_state(version, workspace_id, _ordinary_guard=_ordinary_guard)
                if join is None:
                    owner._validate_local_absence(version, row, join)
                    if target is not None:
                        owner.prepare_local_regrant(version, target, _ordinary_guard=_ordinary_guard)
                else:
                    token = owner.prepare_local_withdrawal(version, workspace_id, _ordinary_guard=_ordinary_guard)
                    row = token.state[0]
                    # Same recipient availability read as original B3, before B2 DML.
                    recipients = tuple(
                        self.session.execute(
                            sa.select(Join.id, Join.account_id)
                            .where(Join.tenant_id == history.workspace_id, Join.role == TenantAccountRole.OWNER)
                            .order_by(Join.id)
                            .limit(2)
                            .with_for_update()
                        )
                    )
                    if len(recipients) != 1 or recipients[0].account_id == str(c.account_id):
                        raise CasdoorLoginScopeConflict()
                    for value in recipients[0]:
                        _uuid(value)
                    withdrawals.append(workspace_id)
                rows.append(row)
            elif view.decision is OwnershipDecision.MANAGED_CURRENT:
                refs = owner._current_local_refs(c.account_id, workspace_id, c.namespace_id, c.identity_id)
                if len(refs) != 1 or refs[0].id != history.id:
                    raise CasdoorLoginScopeConflict()
                rows.append(owner._current_row(refs[0]))
        if len(withdrawals) > 100 or len(rows) > MAX_SCOPE_ROWS:
            raise CasdoorLoginScopeConflict()
        return tuple(sorted(withdrawals, key=str)), tuple(rows)

    def _reread_local_rows(self, rows):
        """Original scalar references and bounded reader, never bulk history TEXT."""
        owner = CasdoorMembershipRepository(self.session)
        current = []
        for row in rows:
            refs = owner._current_local_refs(
                _uuid(row.account_id), _uuid(row.workspace_id), _uuid(row.namespace_id), _uuid(row.identity_id)
            )
            if len(refs) != 1 or refs[0].id != row.id:
                raise CasdoorLoginScopeConflict()
            current.append(owner._current_row(refs[0]))
        return tuple(current)

    def recheck_before_commit(
        self,
        prepared,
        before: LoginScope,
        plan: DesiredWorkspacePlan,
        result: LocalMembershipPersistence,
        *,
        expected_account_name: str | None = None,
        controlled_before=(),
        _ordinary_guard=None,
    ) -> None:
        """Reconstruct full scope and allow only actual owners' explicit local delta."""
        current = self.discover(
            prepared,
            _lock=True,
            _parents=tuple(_uuid(row.id) for row in before.workspaces),
            _ordinary_guard=_ordinary_guard,
        )
        if (
            current._archived_facts != before._archived_facts
            or current.configuration != before.configuration
            or current.namespaces != before.namespaces
            or current.workspaces != before.workspaces
            or current.lease_scope.canonical_keys != before.lease_scope.canonical_keys
        ):
            raise CasdoorLoginScopeConflict()
        self._intent_barrier(prepared.account_id, current, _ordinary_guard=_ordinary_guard)
        fresh = CasdoorAccountPreflightRepository(self.session).reconstruct(
            prepared.plan.context, prepared.key, candidate_account_id=prepared.account_id
        )
        if (
            fresh.account is None
            or fresh.account.status is not AccountStatus.ACTIVE
            or fresh.account.initialized_at is None
            or fresh.exact_binding is None
            or fresh.exact_binding.account_id != result.account_id
        ):
            raise CasdoorLoginScopeConflict()
        account = current.account
        if account is None:
            raise CasdoorLoginScopeConflict()
        if expected_account_name is not None and (
            type(expected_account_name) is not str or account.name != expected_account_name
        ):
            raise CasdoorLoginScopeConflict()
        if prepared.plan.action in (AdmissionAction.CREATE_INITIALIZED, AdmissionAction.INITIALIZE_BOUND):
            setup = prepared.plan.setup
            if (account.name, account.interface_language, account.timezone, account.interface_theme) != (
                setup.name if expected_account_name is None else expected_account_name,
                setup.interface_language,
                setup.timezone,
                "light",
            ):
                raise CasdoorLoginScopeConflict()
        if prepared.plan.action is AdmissionAction.CREATE_INITIALIZED:
            # Original account creation leaves avatar at its model default.
            # A profile call may change only name, never adopt a later avatar.
            if account.avatar is not None:
                raise CasdoorLoginScopeConflict()
            if (account.email, account.normalized_email, account.password_unset, account.password_salt_unset) != (
                prepared.plan.creation_email,
                normalize_email(prepared.plan.creation_email),
                True,
                True,
            ):
                raise CasdoorLoginScopeConflict()
        elif before.account is not None:
            allowed = {"status", "initialized_at"}
            if prepared.plan.action is AdmissionAction.INITIALIZE_BOUND:
                allowed |= {"name", "interface_language", "timezone", "interface_theme"}
            if any(
                getattr(account, name)
                != (expected_account_name if name == "name" and expected_account_name is not None else value)
                for name, value in before.account._mapping.items()
                if name not in allowed
            ):
                raise CasdoorLoginScopeConflict()
            if before.account.initialized_at is not None and account.initialized_at != before.account.initialized_at:
                raise CasdoorLoginScopeConflict()
        expected_identities = {row.id: row for row in before.identities}
        actual_identities = {row.id: row for row in current.identities}
        own = actual_identities.pop(str(result.identity_id), None)
        prior = expected_identities.pop(str(result.identity_id), None)
        if (
            own is None
            or own.sync_generation != result.generation
            or actual_identities != expected_identities
            or (prior is not None and tuple(own)[:-1] != tuple(prior)[:-1])
        ):
            raise CasdoorLoginScopeConflict()
        self._verify_memberships(before, current, plan, result, controlled_before=controlled_before)
        if _ordinary_guard is not None:
            from repositories.casdoor_terminal_local_invitation_repository_extend import _ordinary_terminal_last

            _ordinary_terminal_last(_ordinary_guard)

    def _verify_memberships(self, before, current, plan, result, *, controlled_before=()):
        from services.casdoor_local_membership_service_extend import RequiredIntentBarrier

        old_joins = {row.id: row for row in before.joins}
        new_joins = {row.id: row for row in current.joins}
        old_history = {row.id: row for row in before.histories}
        new_history = {row.id: row for row in current.histories}
        version = GenerationPlanVersion(plan, result.fence_epoch, result.generation)
        owner = CasdoorMembershipRepository(self.session)
        captured = {row.id: row for row in controlled_before}
        if any(type(effect) is not _LocalMembershipEffect for effect in result._controlled_effects):
            raise CasdoorLoginScopeConflict()
        effects = {effect.before.id: effect for effect in result._controlled_effects}
        withdrawals = {str(w.membership_id): w for w in result.withdrawals}
        regrants = {str(w.membership_id): w for w in result.workspaces if w.membership_regranted}
        if (
            len(captured) != len(controlled_before)
            or len(effects) != len(result._controlled_effects)
            or len(withdrawals) != len(result.withdrawals)
            or len(regrants) != sum(w.membership_regranted for w in result.workspaces)
            or set(withdrawals) & set(regrants)
            or set(effects) != set(withdrawals) | set(regrants)
            or len({e.before.workspace_id for e in effects.values()}) != len(effects)
        ):
            raise CasdoorLoginScopeConflict()
        for membership_id, effect in effects.items():
            if captured.get(membership_id) != effect.before:
                raise CasdoorLoginScopeConflict()
            changed = {
                "ownership_epoch",
                "revision_id",
                "desired_generation",
                "last_applied_roles_json",
                "last_applied_fingerprint",
                "desired_roles_json",
                "finalization",
                "updated_at",
            }
            if membership_id in regrants:
                changed.add("join_id")
                summary = regrants[membership_id]
                if summary.ownership_decision is OwnershipDecision.MAPPED_REGRANT_REQUIRED:
                    changed |= {"ownership", "tombstone"}
                    target = next((t for t in plan.targets if t.workspace_id == summary.workspace_id), None)
                    if (
                        target is None
                        or target.reason is not CasdoorDecisionReason.ROLE_MAPPING
                        or not target.matched_role_refs
                        or effect.before.ownership is not CasdoorMembershipOwnership.LOCAL_OVERRIDE
                        or effect.before.tombstone is not True
                        or effect.before.join_id in old_joins
                    ):
                        raise CasdoorLoginScopeConflict()
                elif (
                    summary.ownership_decision is not OwnershipDecision.CONTROLLED_WITHDRAWN
                    or effect.before.ownership is not CasdoorMembershipOwnership.MANAGED
                    or effect.before.tombstone is not False
                ):
                    raise CasdoorLoginScopeConflict()
                if (
                    effect.after.ownership is not CasdoorMembershipOwnership.MANAGED
                    or effect.after.tombstone is not False
                ):
                    raise CasdoorLoginScopeConflict()
            if (
                any(
                    getattr(effect.after, key) != value
                    for key, value in effect.before._asdict().items()
                    if key not in changed
                )
                or effect.after.ownership_epoch != effect.before.ownership_epoch + 1
                or effect.after.desired_generation != result.generation
                or effect.after.revision_id != str(result.revision_id)
                or effect.after.finalization is not CasdoorFinalizationState.PENDING
            ):
                raise CasdoorLoginScopeConflict()
            row = self._reread_local_rows((effect.after,))[0]
            if row != effect.after or owner._snapshot(row) != effect.managed:
                raise CasdoorLoginScopeConflict()
            if membership_id in withdrawals:
                summary = withdrawals[membership_id]
                old = old_joins.pop(effect.before.join_id, None)
                row, join, _ = owner._read_local_state(version, summary.workspace_id)
                owner._validate_local_absence(version, row, join)
                if (
                    old is None
                    or old.id in new_joins
                    or (old.tenant_id, old.account_id, old.role)
                    != (str(summary.workspace_id), str(result.account_id), summary.prior_role)
                    or (effect.before.join_id, effect.before.ownership_epoch, row.ownership_epoch, row.finalization)
                    != (str(summary.removed_join_id), summary.epoch_before, summary.epoch_after, summary.finalization)
                    or old_history.pop(membership_id, None) is None
                    or new_history.pop(membership_id, None) is None
                ):
                    raise CasdoorLoginScopeConflict()
            else:
                owner._require_removed_id_absent(effect.before)
        for target, observation in zip(plan.targets, result.workspaces, strict=True):
            if target.workspace_id != observation.workspace_id:
                raise CasdoorLoginScopeConflict()
            view = owner.inspect(version, target.workspace_id, backend=MembershipBackend.LOCAL)
            if (view.observation.join_id, view.observation.join_role) != (
                observation.join_id,
                observation.current_role,
            ):
                raise CasdoorLoginScopeConflict()
            if observation.outcome not in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP):
                continue
            join = new_joins.pop(str(observation.join_id), None)
            prior_join = old_joins.pop(str(observation.join_id), None)
            history = new_history.pop(str(observation.membership_id), None)
            prior_history = old_history.pop(str(observation.membership_id), None)
            managed = view.managed
            if (
                join is None
                or history is None
                or managed is None
                or view.decision is not OwnershipDecision.MANAGED_CURRENT
                or join.role.value != target.target_role
                or managed.membership_id != observation.membership_id
                or managed.desired_generation != result.generation
                or managed.revision_id != plan.context.revision_id
                or managed.last_applied_roles_json != role_baseline_json(view.observation)
                or managed.last_applied_fingerprint != roles_fingerprint(view.observation)
                or history.finalization is not observation.finalization
            ):
                raise CasdoorLoginScopeConflict()
            desired = {
                "schema_version": 1,
                "backend": "local",
                "target_role": target.target_role,
                "builtin_id": target.builtin_id,
                "role_ids": [target.target_role],
                "reason": target.reason.value,
                "fence_epoch": result.fence_epoch,
            }
            if managed.desired_roles_json != json.dumps(desired, sort_keys=True, separators=(",", ":")):
                raise CasdoorLoginScopeConflict()
            if observation.membership_regranted:
                effect = effects.get(str(observation.membership_id))
                if (
                    observation.membership_created
                    or effect is None
                    or prior_join is not None
                    or prior_history is None
                    or join.invited_by is not None
                    or str(observation.join_id) != effect.after.join_id
                ):
                    raise CasdoorLoginScopeConflict()
            elif observation.membership_created:
                if (
                    prior_join is not None
                    or prior_history is not None
                    or history.finalization is not CasdoorFinalizationState.PENDING
                    or history.ownership_epoch != 0
                    or join.invited_by is not None
                    or managed.baseline_json != role_baseline_json(view.observation)
                    or history.source
                    is not (
                        CasdoorMembershipSource.FALLBACK
                        if target.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                        else CasdoorMembershipSource.MAPPING
                    )
                ):
                    raise CasdoorLoginScopeConflict()
            else:
                if prior_join is None or prior_history is None:
                    raise CasdoorLoginScopeConflict()
                if any(getattr(join, name) != value for name, value in prior_join._mapping.items() if name != "role"):
                    raise CasdoorLoginScopeConflict()
                changed = {"desired_generation", "revision_id", "last_applied_fingerprint"}
                if any(
                    getattr(history, name) != value
                    for name, value in prior_history._mapping.items()
                    if name not in changed
                ):
                    raise CasdoorLoginScopeConflict()
        ordinary = {
            str(w.membership_id): w
            for w in result.workspaces
            if w.outcome in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP)
        }
        for row, actual in zip(controlled_before, self._reread_local_rows(controlled_before), strict=True):
            if row.id in effects:
                if actual != effects[row.id].after:
                    raise CasdoorLoginScopeConflict()
            elif row.id in ordinary:
                allowed = {
                    "revision_id",
                    "desired_generation",
                    "desired_roles_json",
                    "last_applied_roles_json",
                    "last_applied_fingerprint",
                    "updated_at",
                }
                if any(getattr(actual, name) != value for name, value in row._asdict().items() if name not in allowed):
                    raise CasdoorLoginScopeConflict()
            else:
                if actual != row:
                    raise CasdoorLoginScopeConflict()
                preserved = tuple(
                    item
                    for item in result.workspaces
                    if str(item.membership_id) == row.id and str(item.workspace_id) == row.workspace_id
                )
                if (
                    len(preserved) == 1
                    and preserved[0].outcome is LocalRoleOutcome.PRESERVED
                    and preserved[0].ownership_decision
                    in (
                        OwnershipDecision.OWNER_PROTECTED,
                        OwnershipDecision.PRESERVE_OVERRIDE,
                        OwnershipDecision.PRESERVE_UNMANAGED,
                    )
                    and preserved[0].role_changed is False
                    and preserved[0].metadata_changed is False
                    and preserved[0].intent_barrier is RequiredIntentBarrier.CLEAR
                ):
                    item = preserved[0]
                    view = owner.inspect(version, _uuid(row.workspace_id), backend=MembershipBackend.LOCAL)
                    if (
                        view.decision is item.ownership_decision
                        and view.managed is not None
                        and view.managed == owner._snapshot(actual)
                        and view.managed.membership_id == _uuid(row.id)
                        and view.managed.namespace_id == plan.context.namespace_id
                        and view.managed.identity_id == plan.context.identity_id
                        and view.managed.account_id == plan.context.account_id
                        and view.managed.workspace_id == _uuid(row.workspace_id)
                        and view.observation.join_id == item.join_id == _uuid(row.join_id)
                        and view.observation.join_role is item.current_role
                        and view.observation.account_id == plan.context.account_id
                        and view.observation.workspace_id == _uuid(row.workspace_id)
                    ):
                        continue
                absent, join, _ = owner._read_local_state(version, _uuid(row.workspace_id))
                owner._validate_local_absence(version, absent, join)
        if new_joins != old_joins or new_history != old_history:
            raise CasdoorLoginScopeConflict()

    def _invited_nowait_rows(self, statement, *, cap=MAX_SCOPE_ROWS):
        """Bounded NOWAIT locking only for already projected invited parents."""
        self._clean()
        return self._rows(statement.with_for_update(nowait=True), cap=cap)

    def _lock_invited_integration(self, context: AdmissionContext) -> None:
        """First serialization lock for cooperating same-integration Casdoor UoWs."""
        rows = self._invited_nowait_rows(
            sa.select(Integration.id, Integration.enabled, Integration.active_revision_id)
            .where(Integration.slot == 1, Integration.id == str(context.integration_id))
            .order_by(Integration.id),
            cap=1,
        )
        if tuple(tuple(row) for row in rows) != ((str(context.integration_id), True, str(context.revision_id)),):
            raise CasdoorLoginScopeConflict()

    def _lock_invited_candidate_parents(self, context: AdmissionContext, candidate: LoginScope) -> None:
        """Lock only registered IDs after the integration-held second projection.

        This adds no lock of newly discovered parents. Full later non-locking
        projection detects visible expansion; unrelated mutation owners and
        database gap/phantom or global-snapshot guarantees are outside scope.
        """
        namespaces = self._invited_nowait_rows(
            sa.select(
                Namespace.id,
                Namespace.integration_id,
                Namespace.expected_issuer,
                Namespace.organization,
                Namespace.application,
                Namespace.client_id,
                Namespace.lifecycle,
                Namespace.fence_epoch,
            )
            .where(
                Namespace.integration_id == str(context.integration_id),
                Namespace.id.in_([row.id for row in candidate.namespaces]),
            )
            .order_by(Namespace.id),
            cap=MAX_NAMESPACE_ROWS,
        )
        if namespaces != candidate.namespaces:
            raise CasdoorLoginScopeConflict()
        revision = self._invited_nowait_rows(
            sa.select(Revision.id, Revision.integration_id, Revision.namespace_id, Revision.config_digest)
            .where(Revision.id == str(context.revision_id))
            .order_by(Revision.id),
            cap=1,
        )
        if tuple(tuple(row) for row in revision) != (
            (
                str(context.revision_id),
                str(context.integration_id),
                str(context.namespace_id),
                context.config_digest,
            ),
        ):
            raise CasdoorLoginScopeConflict()
        account = self._invited_nowait_rows(
            sa.select(
                Account.id,
                Account.email,
                Account.normalized_email,
                Account.status,
                Account.initialized_at,
                Account.name,
                Account.avatar,
                Account.interface_language,
                Account.timezone,
                Account.interface_theme,
                Account.password.is_(None).label("password_unset"),
                Account.password_salt.is_(None).label("password_salt_unset"),
            )
            .where(Account.id == candidate.account.id)
            .order_by(Account.id),
            cap=1,
        )
        if account != (candidate.account,):
            raise CasdoorLoginScopeConflict()
        identities = self._invited_nowait_rows(
            sa.select(
                Identity.id,
                Identity.namespace_id,
                Identity.account_id,
                Identity.issuer,
                Identity.organization,
                Identity.subject,
                Identity.subject_digest,
                Identity.sync_generation,
            )
            .where(
                Identity.account_id == candidate.account.id,
                sa.tuple_(Identity.namespace_id, Identity.id).in_(
                    [(row.namespace_id, row.id) for row in candidate.identities]
                ),
            )
            .order_by(Identity.namespace_id, Identity.id),
        )
        if identities != candidate.identities:
            raise CasdoorLoginScopeConflict()
        workspaces = self._invited_nowait_rows(
            sa.select(Tenant.id, Tenant.status)
            .where(Tenant.id.in_([row.id for row in candidate.workspaces]))
            .order_by(Tenant.id),
        )
        if workspaces != candidate.workspaces:
            raise CasdoorLoginScopeConflict()
