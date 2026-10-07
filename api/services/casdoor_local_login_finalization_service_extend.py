"""Concrete private LOCAL tail, called only from the successful C2/C1 stack.

Fresh SQL gates cannot eliminate the gap to Redis or serialize legacy writers.
No proof producer, route, Cookie, remote saga, lease lifecycle or retry lives
here. Partial/unissued Redis credentials expire under the original issuer TTL;
account-wide revoke would damage concurrent sessions and is not rollback.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from ipaddress import ip_address as parse_ip_address
from uuid import UUID

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.admission import AdmissionAction
from core.casdoor.auth_transactions import AuthMode
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.leases import CasdoorLeases
from core.casdoor.local_roles import LOCAL_ROLES, LocalRoleOutcome
from core.casdoor.mapping import MappingIdentityContext, resolve_workspace_plan
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from enums import DeploymentEdition
from libs.datetime_utils import naive_utc_now
from models.account import Account, AccountStatus, Tenant, TenantStatus
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorFinalizationState as Finalization
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorMembershipSource
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict, CasdoorLoginScopeRepository
from repositories.casdoor_membership_repository_extend import CasdoorMembershipRepository
from sqlalchemy.orm import Session

from services.account_adapters import BillingWorkspaceMembershipCache
from services.account_login_adapters import RedisAccountSessionGateway
from services.account_service import AccountService
from services.casdoor_local_membership_service_extend import LocalMembershipPersistence, RequiredIntentBarrier
from services.casdoor_login_account_service_extend import _check_deadline, _complete, _PreparedLoginAccount
from services.entities.account_login_entities import AuthTokenPair


class CasdoorLocalLoginFinalizationService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        configuration_factory,
        session_gateway: RedisAccountSessionGateway,
        production_guard: Callable[[], object] | None = None,
    ) -> None:
        if type(session_gateway) is not RedisAccountSessionGateway:
            raise CasdoorLoginScopeConflict()
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory
        self._session_gateway = session_gateway
        self._production_guard = production_guard

    @staticmethod
    def _metadata(session, account_id):
        account = session.execute(
            sa.select(Account.last_login_at, Account.last_login_ip).where(Account.id == str(account_id))
        ).one()
        joins = tuple(
            session.execute(
                sa.select(Join.id, Join.current, Join.last_opened_at)
                .where(Join.account_id == str(account_id))
                .order_by(Join.id)
                .limit(2049)
            )
        )
        if len(joins) > 2048:
            raise CasdoorLoginScopeConflict()
        return tuple(account), tuple(tuple(row) for row in joins)

    @staticmethod
    def _mapping(scope, prepared, persisted, roles):
        c = prepared.plan.context
        context = MappingIdentityContext(
            c.integration_id,
            c.revision_id,
            c.namespace_id,
            persisted.identity_id,
            prepared.account_id,
            c.config_digest,
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.subject,
        )
        return resolve_workspace_plan(
            configuration=scope.configuration, snapshot=roles, context=context, availability=scope.availability()
        )

    @staticmethod
    def _members(session, scope, plan, persisted, *, _ordinary_guard=None):
        """Freeze bounded targets and legitimate prior-only controlled absences."""
        if tuple(t.workspace_id for t in plan.targets) != tuple(w.workspace_id for w in persisted.workspaces):
            raise CasdoorLoginScopeConflict()
        owner = CasdoorMembershipRepository(session)
        version = GenerationPlanVersion(plan, persisted.fence_epoch, persisted.generation)
        snapshots, rows = [], []
        for target, observation in zip(plan.targets, persisted.workspaces, strict=True):
            view = owner.inspect(version, target.workspace_id, backend=MembershipBackend.LOCAL)
            actual = view.observation
            if (
                observation.intent_barrier is not RequiredIntentBarrier.CLEAR
                or actual.join_id is None
                or (actual.join_id, actual.join_role) != (observation.join_id, observation.current_role)
            ):
                raise CasdoorLoginScopeConflict()
            if observation.outcome is LocalRoleOutcome.PRESERVED:
                if view.decision != observation.ownership_decision or view.decision not in (
                    OwnershipDecision.PRESERVE_UNMANAGED,
                    OwnershipDecision.PRESERVE_OVERRIDE,
                    OwnershipDecision.OWNER_PROTECTED,
                ):
                    raise CasdoorLoginScopeConflict()
                snapshots.append(view)
                continue
            managed = view.managed
            history = next((h for h in scope.histories if h.id == str(observation.membership_id)), None)
            if (
                observation.outcome not in (LocalRoleOutcome.APPLIED, LocalRoleOutcome.NOOP)
                or view.decision is not OwnershipDecision.MANAGED_CURRENT
                or managed is None
                or history is None
                or managed.membership_id != observation.membership_id
                or managed.namespace_id != persisted.namespace_id
                or managed.identity_id != persisted.identity_id
                or managed.account_id != persisted.account_id
                or managed.workspace_id != target.workspace_id
                or managed.join_id != actual.join_id
                or managed.desired_generation != persisted.generation
                or managed.revision_id != persisted.revision_id
                or managed.tombstone
                or actual.join_role.value != target.target_role
                or managed.last_applied_roles_json != role_baseline_json(actual)
                or managed.last_applied_fingerprint != roles_fingerprint(actual)
                or history.finalization not in (Finalization.PENDING, Finalization.FINALIZED)
            ):
                raise CasdoorLoginScopeConflict()
            refs = owner._current_local_refs(
                persisted.account_id, target.workspace_id, persisted.namespace_id, persisted.identity_id
            )
            if len(refs) != 1 or refs[0].id != history.id:
                raise CasdoorLoginScopeConflict()
            row = owner._current_row(refs[0])
            if owner._snapshot(row) != managed or any(
                getattr(row, name) != value for name, value in history._mapping.items()
            ):
                raise CasdoorLoginScopeConflict()
            rows.append(row)
            if observation.membership_created and (
                managed.ownership_epoch != 0
                or managed.baseline_json != role_baseline_json(actual)
                or view.invited_by is not None
                or managed.source
                is not (
                    CasdoorMembershipSource.FALLBACK
                    if target.reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                    else CasdoorMembershipSource.MAPPING
                )
            ):
                raise CasdoorLoginScopeConflict()
            for name, value in history._mapping.items():
                if name == "finalization":
                    continue
                expected = getattr(managed, "membership_id" if name == "id" else name)
                if isinstance(expected, UUID):
                    expected = str(expected)
                if value != expected:
                    raise CasdoorLoginScopeConflict()
            baseline = parse_role_baseline_json(managed.baseline_json)
            if (
                baseline.backend is not MembershipBackend.LOCAL
                or baseline.join_role not in (None, *LOCAL_ROLES)
                or baseline.roles
            ):
                raise CasdoorLoginScopeConflict()
            if baseline.join_role is None:
                if observation.membership_created or row.ownership_epoch < 2:
                    raise CasdoorLoginScopeConflict()
                checked, join, _ = owner.read_present_local_state(version, target, _ordinary_guard=_ordinary_guard)
                empty = MembershipObservation(
                    target.workspace_id, persisted.account_id, None, None, MembershipBackend.LOCAL
                )
                if (
                    checked != row
                    or join is None
                    or join.id != row.join_id
                    or row.baseline_json != role_baseline_json(empty)
                ):
                    raise CasdoorLoginScopeConflict()
            desired = {
                "schema_version": 1,
                "backend": "local",
                "target_role": target.target_role,
                "builtin_id": target.builtin_id,
                "role_ids": [target.target_role],
                "reason": target.reason.value,
                "fence_epoch": persisted.fence_epoch,
            }
            if managed.desired_roles_json != json.dumps(desired, sort_keys=True, separators=(",", ":")):
                raise CasdoorLoginScopeConflict()
            snapshots.append(view)
        target_ids = {str(target.workspace_id) for target in plan.targets}
        for history in scope.histories:
            if (
                history.workspace_id in target_ids
                or history.namespace_id != str(persisted.namespace_id)
                or history.identity_id != str(persisted.identity_id)
                or history.ownership is not CasdoorMembershipOwnership.MANAGED
                or history.tombstone
            ):
                continue
            row, join, _ = owner._read_local_state(version, UUID(history.workspace_id), _ordinary_guard=_ordinary_guard)
            owner._validate_local_absence(version, row, join)
            if any(getattr(row, name) != value for name, value in history._mapping.items()):
                raise CasdoorLoginScopeConflict()
            rows.append(row)
        if len({r.workspace_id for r in rows}) != len(rows):
            raise CasdoorLoginScopeConflict()
        return tuple(snapshots), tuple(sorted(rows, key=lambda r: r.workspace_id))

    @staticmethod
    def _account(scope, prepared, persisted):
        account = scope.account
        c = prepared.plan.context
        exact = [r for r in scope.identities if r.namespace_id == str(c.namespace_id)]
        if (
            account is None
            or account.status is not AccountStatus.ACTIVE
            or account.initialized_at is None
            or len(exact) != 1
            or tuple(exact[0])
            != (
                str(persisted.identity_id),
                str(c.namespace_id),
                str(persisted.account_id),
                c.issuer,
                c.organization,
                c.subject,
                prepared.key.subject_digest,
                persisted.generation,
            )
        ):
            raise CasdoorLoginScopeConflict()
        old = prepared.preflight.account
        if old is not None and (
            account.email != old.email
            or (old.initialized_at is not None and account.initialized_at != old.initialized_at)
        ):
            raise CasdoorLoginScopeConflict()
        if prepared.plan.action in (AdmissionAction.CREATE_INITIALIZED, AdmissionAction.INITIALIZE_BOUND):
            setup = prepared.plan.setup
            if (account.interface_language, account.timezone, account.interface_theme) != (
                setup.interface_language,
                setup.timezone,
                "light",
            ):
                raise CasdoorLoginScopeConflict()

    @staticmethod
    def _finalize(session, row):
        values = row._asdict()
        predicates = []
        for name, value in values.items():
            column = getattr(History, name)
            if isinstance(value, UUID):
                value = str(value)
            predicates.append(column.is_(None) if value is None else column == value)
        result = session.execute(
            sa.update(History)
            .where(*predicates, History.finalization == Finalization.PENDING)
            .values(finalization=Finalization.FINALIZED)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CasdoorLoginScopeConflict()

    @staticmethod
    def _login_metadata(session, account_id, ip_address):
        """Original load_user selection policy, with caller-owned flush/commit."""
        account = session.get(Account, str(account_id))
        current = session.scalar(sa.select(Join).where(Join.account_id == account.id, Join.current.is_(True)).limit(1))
        tenant = session.get(Tenant, current.tenant_id) if current is not None else None
        if tenant is None or tenant.status is not TenantStatus.NORMAL:
            if current is not None:
                current.current = False
            current = session.scalar(
                sa.select(Join)
                .join(Tenant, Join.tenant_id == Tenant.id)
                .where(Join.account_id == account.id, Tenant.status == TenantStatus.NORMAL)
                .order_by(Join.id.asc())
                .limit(1)
            )
            if current is None:
                raise CasdoorLoginScopeConflict()
            current.current = True
            current.last_opened_at = naive_utc_now()
            tenant = session.get(Tenant, current.tenant_id)
        account.set_current_tenant_with_session(tenant, session=session)
        if account.current_tenant_id != current.tenant_id or account.role != current.role:
            raise CasdoorLoginScopeConflict()
        AccountService.persist_login_info(account, ip_address=ip_address)
        # Build expected metadata from actual assignments before flush/triggers.
        return current.id

    def _finalize_local_login(
        self, *, prepared, persisted, roles, leases, configuration, ip_address, _ordinary_attempt=None
    ) -> AuthTokenPair:
        """One cache pass, one write root and one final read, never replay C1.

        Exception facts distinguish attempted commit and token I/O; neither
        scalar is a capability. Public persistence objects cannot authenticate.
        """
        phase, tokens = "not_started", "not_started"
        try:
            if (
                not _complete(prepared, _PreparedLoginAccount)
                or type(persisted) is not LocalMembershipPersistence
                or type(leases) is not CasdoorLeases
                or prepared.plan.mode is not AuthMode.LOGIN
                or prepared.plan.action
                not in (
                    AdmissionAction.CREATE_INITIALIZED,
                    AdmissionAction.INITIALIZE_BOUND,
                    AdmissionAction.ACTIVATE_BOUND,
                    AdmissionAction.USE_BOUND,
                )
                or type(ip_address) is not str
                or not ip_address
                or len(ip_address) > 45
                or leases._deadline != prepared.deadline
                or not 0 < persisted.generation
                or not 0 < len(persisted.workspaces) <= 100
                or (persisted.account_id, persisted.namespace_id, persisted.revision_id, persisted.fence_epoch)
                != (
                    prepared.account_id,
                    prepared.plan.context.namespace_id,
                    prepared.plan.context.revision_id,
                    prepared.plan.context.fence_epoch,
                )
            ):
                raise CasdoorLoginScopeConflict()

            try:
                parse_ip_address(ip_address)
            except ValueError:
                raise CasdoorLoginScopeConflict() from None

            def guard():
                if dify_config.RBAC_ENABLED is not False:
                    raise CasdoorLoginScopeConflict()
                _check_deadline(prepared.deadline)
                if _ordinary_attempt is not None:
                    from repositories.casdoor_terminal_local_invitation_repository_extend import _ordinary_attempt_check

                    _ordinary_attempt_check(_ordinary_attempt, prepared, roles, leases)
                if self._production_guard is not None:
                    self._production_guard()
                leases.ensure_owned()
                _check_deadline(prepared.deadline)

            def check(session, scope):
                if (
                    scope._archived_facts != persisted._archived_facts
                    or scope.configuration != configuration
                    or scope.lease_scope.canonical_keys != leases.canonical_keys
                ):
                    raise CasdoorLoginScopeConflict()
                self._account(scope, prepared, persisted)
                plan = self._mapping(scope, prepared, persisted, roles)
                return self._members(session, scope, plan, persisted, _ordinary_guard=ordinary_guard)

            from repositories.casdoor_terminal_local_invitation_repository_extend import (
                _bind_ordinary_terminal_root,
                _ordinary_terminal_last,
            )

            guard()
            with self._session_factory() as session, session.begin():
                ordinary_guard = (
                    _bind_ordinary_terminal_root(_ordinary_attempt, session, self._configuration_factory)
                    if _ordinary_attempt is not None
                    else None
                )
                owner = CasdoorLoginScopeRepository(session, self._configuration_factory)
                before = owner.prelock_and_recheck(prepared, owner.discover(prepared), _ordinary_guard=ordinary_guard)
                views, rows = check(session, before)
                metadata = self._metadata(session, persisted.account_id)
                guard()
                _ordinary_terminal_last(ordinary_guard)
            cache = BillingWorkspaceMembershipCache(enabled=dify_config.DEPLOYMENT_EDITION == DeploymentEdition.CLOUD)
            for row in rows:
                if row.finalization is Finalization.PENDING:
                    guard()
                    cache.invalidate(row.workspace_id)
                    guard()
            guard()
            with self._session_factory() as session, session.begin():
                ordinary_guard = (
                    _bind_ordinary_terminal_root(_ordinary_attempt, session, self._configuration_factory)
                    if _ordinary_attempt is not None
                    else None
                )
                owner = CasdoorLoginScopeRepository(session, self._configuration_factory)
                scope = owner.prelock_and_recheck(prepared, before, _ordinary_guard=ordinary_guard)
                if check(session, scope) != (views, rows) or self._metadata(session, persisted.account_id) != metadata:
                    raise CasdoorLoginScopeConflict()
                phase = "not_committed"
                changed = set()
                for row in rows:
                    if row.finalization is Finalization.PENDING:
                        self._finalize(session, row)
                        changed.add(row.id)
                completed_rows = []
                for row, actual in zip(rows, owner._reread_local_rows(rows), strict=True):
                    expected = row._replace(finalization=Finalization.FINALIZED) if row.id in changed else row
                    if row.id in changed:
                        # Only the original model timestamp can differ from our SET delta.
                        if actual._replace(updated_at=expected.updated_at) != expected:
                            raise CasdoorLoginScopeConflict()
                        expected = expected._replace(updated_at=actual.updated_at)
                    elif actual != expected:
                        raise CasdoorLoginScopeConflict()
                    completed_rows.append(expected)
                completed_rows = tuple(completed_rows)
                # Disable autoflush so the expected assignment tuple is captured
                # before SQL triggers; explicitly flush all changes just once.
                with session.no_autoflush:
                    selected = self._login_metadata(session, persisted.account_id, ip_address)
                    # SQL projections do not see pending ORM assignments.
                    account = session.get(Account, str(persisted.account_id))
                    expected_metadata = (
                        (account.last_login_at, account.last_login_ip),
                        tuple(
                            (row.id, row.current, row.last_opened_at)
                            for row in session.scalars(
                                sa.select(Join).where(Join.account_id == account.id).order_by(Join.id).limit(2049)
                            )
                        ),
                    )
                session.flush()
                after = owner.discover(prepared)
                expected_histories = tuple(
                    tuple(
                        Finalization.FINALIZED if name == "finalization" and row.id in changed else value
                        for name, value in row._mapping.items()
                    )
                    for row in before.histories
                )
                if (
                    replace(after, histories=before.histories) != before
                    or tuple(tuple(r) for r in after.histories) != expected_histories
                    or check(session, after) != (views, completed_rows)
                    or self._metadata(session, persisted.account_id) != expected_metadata
                ):
                    raise CasdoorLoginScopeConflict()
                owner._intent_barrier(persisted.account_id, after, _ordinary_guard=ordinary_guard)
                guard()
                _ordinary_terminal_last(ordinary_guard)
                phase = "unknown"
            phase = "committed"
            guard()
            with self._session_factory() as session, session.begin():
                ordinary_guard = (
                    _bind_ordinary_terminal_root(_ordinary_attempt, session, self._configuration_factory)
                    if _ordinary_attempt is not None
                    else None
                )
                owner = CasdoorLoginScopeRepository(session, self._configuration_factory)
                scope = owner.prelock_and_recheck(prepared, after, _ordinary_guard=ordinary_guard)
                if (
                    check(session, scope) != (views, completed_rows)
                    or self._metadata(session, persisted.account_id) != expected_metadata
                ):
                    raise CasdoorLoginScopeConflict()
                selected_join = session.get(Join, selected)
                tenant = session.get(Tenant, selected_join.tenant_id)
                if selected_join.current is not True or tenant.status is not TenantStatus.NORMAL:
                    raise CasdoorLoginScopeConflict()
                guard()
                _ordinary_terminal_last(ordinary_guard)
            guard()
            tokens = "unknown"
            pair = self._session_gateway.issue(str(persisted.account_id))
            tokens = "issued"
            guard()
            return pair
        except BaseException as error:
            error.finalization_outcome = phase
            error.token_outcome = tokens
            raise
