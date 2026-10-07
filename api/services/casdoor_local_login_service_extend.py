"""Private, unmounted ordinary LOCAL login root transaction; no session issuance.

C2 must call real authentication/fresh role owners and acquire these actual leases
outside this service. Public observations are consistency inputs, not evidence of
signed/online authentication. This service has no production factory or route.
Redis ensure_owned is a deliberate consistency check inside the root; F06 and DB
runtime owners still owe physical I/O/lock/commit budgets. Logical deadline checks
cannot interrupt a blocked socket/SQL operation. Commit errors may be unknown;
there is no automatic commit retry or claim that an unknown commit rolled back.
"""

from collections.abc import Callable
from datetime import datetime
from dataclasses import replace
from uuid import UUID

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.admission import AdmissionAction
from core.casdoor.auth_transactions import AuthMode
from core.casdoor.claims import VerifiedProfile
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope
from core.casdoor.local_roles import LocalRoleOutcome
from core.casdoor.mapping import MappingIdentityContext, resolve_workspace_plan
from core.casdoor.request_safety import ProfileAuditResult, ProfileNameReason
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from repositories.casdoor_account_preflight_repository_extend import (
    CasdoorAccountPreflightRepository,
    CollisionKnowledge,
)
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
    LoginScope,
)
from repositories.casdoor_profile_repository_extend import (
    CasdoorProfileRepository,
    ProfilePersistenceOutcome,
    RemoteEmailStatus,
    _clock,
    _name,
    _parse_time,
    _snapshot,
)
from sqlalchemy.orm import Session

from services.casdoor_local_membership_service_extend import (
    CasdoorLocalMembershipService,
    LocalMembershipPersistence,
    RequiredIntentBarrier,
)
from services.casdoor_login_account_service_extend import (
    CasdoorLoginAccountService,
    _check_consistency,
    _check_deadline,
    _complete,
    _PreparedLoginAccount,
)


class CasdoorLocalLoginService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        configuration_factory: Callable[[Session], CasdoorConfigurationRepository],
    ):
        self._session_factory = session_factory
        self._configuration_factory = configuration_factory

    def _persist_local_login(
        self,
        *,
        account_owner: CasdoorLoginAccountService,
        prepared: _PreparedLoginAccount,
        discovered: LoginScope,
        roles: EffectiveRoleSnapshot,
        leases: CasdoorLeases,
        profile: VerifiedProfile | None = None,
        auth_started_at: datetime | None = None,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
        invitation: object = None,
        source: object = None,
        _ordinary_attempt=None,
    ) -> LocalMembershipPersistence:
        """Persist actual B2A -> B3A -> optional I22 -> F12 in one fresh root.

        No lease acquire/release here: C2 owns finally cleanup, including unknown
        commit. The original preparation is consumed only by its issuer, once;
        only a successful CREATE in this function stack grants own-UUID exclusion.
        Returned scalar observations describe committed local rows, never access,
        token, Cookie, finalized membership, or authenticated provider admission.
        """
        if (
            dify_config.RBAC_ENABLED is not False
            or invitation is not None
            or source is not None
            or type(account_owner) is not CasdoorLoginAccountService
            or not _complete(prepared, _PreparedLoginAccount)
            or not _complete(discovered, LoginScope)
            or type(leases) is not CasdoorLeases
        ):
            raise CasdoorLoginScopeConflict()
        try:
            _check_consistency(prepared.plan, prepared.preflight)
        except (AttributeError, TypeError):
            raise CasdoorLoginScopeConflict() from None
        if (
            not _complete(discovered.lease_scope, CasdoorLeaseScope)
            or not _complete(prepared.key, VerifiedIdentityKey)
            or type(prepared.account_id) is not UUID
            or not all(hasattr(leases, field) for field in ("_deadline", "_keys", "_client", "_held", "_started"))
            or prepared.plan.mode is not AuthMode.LOGIN
            or prepared.plan.action
            not in (
                AdmissionAction.CREATE_INITIALIZED,
                AdmissionAction.INITIALIZE_BOUND,
                AdmissionAction.ACTIVATE_BOUND,
                AdmissionAction.USE_BOUND,
            )
            or leases.canonical_keys != discovered.lease_scope.canonical_keys
            or leases._deadline != prepared.deadline
        ):
            raise CasdoorLoginScopeConflict()

        if any(value is not None for value in (profile, auth_started_at, correlation_id, now)):
            if (
                not _complete(profile, VerifiedProfile)
                or type(profile.subject) is not str
                or profile.subject != prepared.plan.context.subject
                or any(
                    value is not None and type(value) is not str
                    for value in (profile.email, profile.name, profile.locale, profile.zoneinfo)
                )
                or (profile.email_verified is not None and type(profile.email_verified) is not bool)
                or type(correlation_id) is not UUID
            ):
                raise CasdoorLoginScopeConflict()
            # Reuse the profile owner's exact value and UTC rules before any SQL.
            _name(profile.name)
            if _clock(auth_started_at) > _clock(now):
                raise CasdoorLoginScopeConflict()

        def guard():
            if dify_config.RBAC_ENABLED is not False:
                raise CasdoorLoginScopeConflict()
            _check_deadline(prepared.deadline)
            if _ordinary_attempt is not None:
                from repositories.casdoor_terminal_local_invitation_repository_extend import _ordinary_attempt_check

                _ordinary_attempt_check(_ordinary_attempt, prepared, roles, leases)
            leases.ensure_owned()
            _check_deadline(prepared.deadline)

        def mapping(scope, identity_id):
            c = prepared.plan.context
            context = MappingIdentityContext(
                c.integration_id,
                c.revision_id,
                c.namespace_id,
                identity_id,
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

        guard()
        with self._session_factory() as session:
            if (
                not isinstance(session, Session)
                or session.in_transaction()
                or not session.is_active
                or session.new
                or session.dirty
                or session.deleted
            ):
                raise CasdoorLoginScopeConflict()
            with session.begin():
                root = session.get_transaction()
                ordinary_guard = None
                if _ordinary_attempt is not None:
                    from repositories.casdoor_terminal_local_invitation_repository_extend import (
                        _bind_ordinary_terminal_root,
                        _ordinary_terminal_last,
                    )

                    ordinary_guard = _bind_ordinary_terminal_root(_ordinary_attempt, session, self._configuration_factory)
                repository = CasdoorLoginScopeRepository(session, self._configuration_factory)
                ordinary_kwargs = {"_ordinary_guard": ordinary_guard} if ordinary_guard is not None else {}
                before = repository.prelock_and_recheck(prepared, discovered, **ordinary_kwargs)
                # Old-scope removal must be noticed before even account activation.
                current_identity = next(
                    (row for row in before.identities if row.namespace_id == str(prepared.key.namespace_id)), None
                )
                withdrawal_ids, controlled_before = (), ()
                if current_identity is not None:
                    withdrawal_ids, controlled_before = repository._classify_local_changes(
                        before, mapping(before, UUID(current_identity.id)), **ordinary_kwargs
                    )
                elif before.histories:
                    raise CasdoorLoginScopeConflict()
                guard()
                account = account_owner.persist_login_account(
                    prepared, session=session, context=prepared.plan.context, key=prepared.key
                )
                session.flush()
                guard()
                if session.get_transaction() is not root or account.account_id != prepared.account_id:
                    raise CasdoorLoginScopeConflict()
                if repository._reread_local_rows(controlled_before) != controlled_before:
                    raise CasdoorLoginScopeConflict()
                plan = mapping(before, account.identity_id)
                result = CasdoorLocalMembershipService(session).persist_local_memberships(
                    plan,
                    expected_fence_epoch=prepared.plan.context.fence_epoch,
                    expected_generation=current_identity.sync_generation if current_identity else 0,
                    withdrawal_workspace_ids=withdrawal_ids,
                    correlation_id=correlation_id,
                    **ordinary_kwargs,
                )
                session.flush()
                guard()
                if any(
                    item.outcome is LocalRoleOutcome.PENDING or item.intent_barrier is RequiredIntentBarrier.PENDING
                    for item in result.workspaces
                ):
                    raise CasdoorLoginScopeConflict()
                expected_name = None
                if profile is not None:
                    expected_name = (
                        prepared.plan.setup.name
                        if prepared.plan.action in (AdmissionAction.CREATE_INITIALIZED, AdmissionAction.INITIALIZE_BOUND)
                        else before.account.name
                    )
                    profile_outcome = CasdoorProfileRepository(
                        session, configuration_repository=self._configuration_factory(session)
                    ).persist(
                        plan.context,
                        profile,
                        expected_generation=result.generation,
                        expected_fence_epoch=result.fence_epoch,
                        auth_started_at=auth_started_at,
                        admission=prepared.plan,
                        correlation_id=correlation_id,
                        now=now,
                    )
                    session.flush()
                    if (
                        not _complete(profile_outcome, ProfilePersistenceOutcome)
                        or profile_outcome.identity_id != result.identity_id
                        or profile_outcome.account_id != result.account_id
                        or type(profile_outcome.generation) is not int
                        or profile_outcome.generation != result.generation
                        or type(profile_outcome.snapshot_changed) is not bool
                        or type(profile_outcome.name_status) is not ProfileAuditResult
                        or type(profile_outcome.name_reason) is not ProfileNameReason
                        or type(profile_outcome.remote_email_status) is not RemoteEmailStatus
                        or type(profile_outcome.remote_email_differs) is not bool
                    ):
                        raise CasdoorLoginScopeConflict()

                    def read_profile():
                        # Only the current identity's bounded snapshots; never history TEXT.
                        row = session.execute(
                            sa.select(
                                Account.name,
                                Identity.last_applied_json,
                                Identity.profile_sync_json,
                                Identity.remote_profile_version,
                                Identity.remote_email,
                                Identity.email_verified,
                                Identity.last_seen_at,
                            )
                            .join(Account, Account.id == Identity.account_id)
                            .where(Identity.id == str(result.identity_id), Account.id == str(result.account_id))
                        ).one_or_none()
                        if row is None:
                            raise CasdoorLoginScopeConflict()
                        baseline = _snapshot(row.last_applied_json, baseline=True, generation=result.generation)
                        sync = _snapshot(row.profile_sync_json, baseline=False, generation=result.generation)
                        return row, baseline, sync

                    post_profile, baseline, sync = read_profile()
                    if profile_outcome.name_status is ProfileAuditResult.APPLIED:
                        reason = profile_outcome.name_reason
                        if reason is ProfileNameReason.CREATED_BASELINE:
                            if (
                                prepared.plan.action is not AdmissionAction.CREATE_INITIALIZED
                                or expected_name != profile.name
                            ):
                                raise CasdoorLoginScopeConflict()
                        elif reason in (ProfileNameReason.FILLED_EMPTY, ProfileNameReason.MANAGED_UPDATE):
                            if (
                                (reason is ProfileNameReason.FILLED_EMPTY and expected_name.strip())
                                or (
                                    reason is ProfileNameReason.MANAGED_UPDATE
                                    and before.configuration.name_sync != "managed"
                                )
                                or _name(profile.name) is None
                            ):
                                raise CasdoorLoginScopeConflict()
                            expected_name = profile.name
                        else:
                            raise CasdoorLoginScopeConflict()
                        if baseline.get("name") != profile.name or baseline.get("name_generation") != result.generation:
                            raise CasdoorLoginScopeConflict()
                    if (
                        post_profile.name != expected_name
                        or not sync
                        or _parse_time(sync["last_sync_at"]) != profile_outcome.synced_at
                    ):
                        raise CasdoorLoginScopeConflict()
                    if profile_outcome.snapshot_changed and (
                        sync["generation"] != result.generation
                        or _parse_time(sync["auth_started_at"]) != auth_started_at
                        or sync["correlation_id"] != str(correlation_id)
                        or _parse_time(sync["last_sync_at"]) != now
                        or sync["name_status"] != profile_outcome.name_status.value
                        or sync["name_reason"] != profile_outcome.name_reason.value
                        or sync["remote_email_status"] != profile_outcome.remote_email_status.value
                        or sync["remote_email_differs"] != profile_outcome.remote_email_differs
                        or post_profile.last_seen_at != now.replace(tzinfo=None)
                    ):
                        raise CasdoorLoginScopeConflict()
                    guard()
                    CasdoorAvatarRepository(
                        session, configuration_repository=self._configuration_factory(session)
                    ).persist_pending(
                        plan.context,
                        profile,
                        profile_outcome,
                        expected_generation=result.generation,
                        expected_fence_epoch=result.fence_epoch,
                        auth_started_at=auth_started_at,
                        correlation_id=correlation_id,
                        now=now,
                    )
                    session.flush()
                    guard()
                if prepared.plan.action is AdmissionAction.CREATE_INITIALIZED:
                    # No synthetic result or old binding can reach here without
                    # this actual original issued CREATE succeeding in this root.
                    collision = CasdoorAccountPreflightRepository(session)._observe_postwrite_new_collisions(
                        prepared.plan.context,
                        prepared.key,
                        own_new_account_id=prepared.account_id,
                        collision_email=prepared.plan.creation_email,
                    )
                    if collision.collision_knowledge is not CollisionKnowledge.COMPLETE or collision.collision_account_ids:
                        raise CasdoorLoginScopeConflict()
                guard()
                controlled_kwargs = {"controlled_before": controlled_before} if controlled_before else {}
                controlled_kwargs.update(ordinary_kwargs)
                if profile is None:
                    repository.recheck_before_commit(prepared, before, plan, result, **controlled_kwargs)
                else:
                    repository.recheck_before_commit(
                        prepared, before, plan, result, expected_account_name=expected_name, **controlled_kwargs
                    )
                    if read_profile()[0] != post_profile:
                        raise CasdoorLoginScopeConflict()
                result = replace(result, _archived_facts=before._archived_facts)
                if session.get_transaction() is not root or session.new or session.dirty or session.deleted:
                    raise CasdoorLoginScopeConflict()
                # Last operation before context-manager root commit; no writes
                # or external provider work can be scheduled beyond this barrier.
                guard()
                if ordinary_guard is not None:
                    _ordinary_terminal_last(ordinary_guard)
            return result
