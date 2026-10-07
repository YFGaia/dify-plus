"""Actual same-attempt invited LOCAL operation and optional concrete session tail.

The production request binds the real issuer; private offline operation-only
composition stays pending. Original configuration, claims, scope locks and
cleanup owners are reused. A projection or receipt never replaces actual caller
authentication. Mounted HTTP constructs this owner after the original consume
and native exchange. The first LOCAL domain admits no prior managed history or
other non-avatar intent; mixed, unknown and RBAC-enabled scopes stay pending.
"""

from copy import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address as parse_ip_address
from uuid import UUID, uuid4
from weakref import WeakSet

import sqlalchemy as sa
from configs import dify_config
from core.casdoor.admission import AdmissionAction, InvitationObservation, decide_admission
from core.casdoor.auth_transactions import AuthMode, ConsumedAuthTransaction
from core.casdoor.claims import VerifiedOnlineUser
from core.casdoor.gateway import CasdoorDirectoryGateway, GatewayOperation, RawTokens
from core.casdoor.leases import CasdoorLeases
from core.casdoor.mapping import MappingIdentityContext
from core.casdoor.request_safety import ProfileAuditResult, ProfileNameReason
from core.casdoor.role_graph import OnlineRoleSnapshotLoader, _contract
from enums import DeploymentEdition
from libs.helper import email as validate_email
from models.account import (
    Account,
    AccountIntegrate,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantStatus,
)
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_account_preflight_repository_extend import (
    CasdoorAccountPreflightRepository,
)
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
)
from repositories.casdoor_invited_finalization_receipt_repository_extend import (
    CasdoorInvitedFinalizationReceiptRepository,
)
from repositories.casdoor_invited_login_scope_repository_extend import (
    CasdoorInvitedLoginScopeRepository,
)
from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeRepository,
)
from repositories.casdoor_profile_repository_extend import (
    CasdoorProfileRepository,
    ProfilePersistenceOutcome,
    _dump,
    _name,
    _parse_time,
    _snapshot,
)

from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.account_login_adapters import RedisAccountSessionGateway
from services.casdoor_invitation_finalization_service_extend import (
    CasdoorInvitationFinalizationService,
)
from services.casdoor_invitation_operation_service_extend import (
    CasdoorInvitationOperationService,
)
from services.casdoor_invited_local_finalization_service_extend import (
    CasdoorInvitedLocalFinalizationService,
)
from services.casdoor_invited_local_membership_service_extend import (
    CasdoorInvitedLocalMembershipService,
)
from services.casdoor_invited_login_account_service_extend import (
    CasdoorInvitedLoginAccountService,
)
from services.casdoor_local_login_coordinator_service_extend import (
    CasdoorLocalLoginCoordinatorService,
    _CoordinationConflict,
)
from services.casdoor_local_login_finalization_service_extend import (
    CasdoorLocalLoginFinalizationService,
)
from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
from services.casdoor_session_service_extend import SessionProvenanceSeed
from services.casdoor_signing_validator_service_extend import create_claims_validator
from services.entities.account_activation_entities import InvitationLookup
from services.entities.account_login_entities import AuthTokenPair


@dataclass(frozen=True, repr=False)
class _InvitedLocalLoginOutcome:
    operation: object
    cleanup_released: bool
    local_outcome: str = "committed"
    finalization_outcome: str = "not_started"
    token_outcome: str = "not_started"
    tokens: AuthTokenPair | None = None
    provenance: SessionProvenanceSeed | None = None


_ISSUED_OPERATION_GUARDS: WeakSet = WeakSet()
_ISSUED_FINALIZATION_GUARDS: WeakSet = WeakSet()
_ISSUED_CONTINUATIONS: WeakSet = WeakSet()
_ISSUED_RECOVERY_CONTINUATIONS: WeakSet = WeakSet()
_ISSUED_SESSION_TAILS: WeakSet = WeakSet()
_ISSUED_RECOVERY_FINALIZATION_GUARDS: WeakSet = WeakSet()


class _InvitationScopeDrift(Exception):
    pass


class _InvitedOperationGuard:
    """Same-stack one-root guard, issued only after the actual verified caller."""

    def __init__(
        self,
        caller,
        consumed,
        operation,
        observation,
        discovered,
        prepared,
        remote_email,
    ):
        self.caller, self.consumed, self.operation = caller, consumed, operation
        self.observation, self.discovered, self.prepared = (
            observation,
            discovered,
            prepared,
        )
        self.remote_email = remote_email
        self.root = None
        self.expected_scope = discovered

    def ensure(self):
        self.operation._check()
        self.caller._base._require_production_policy()
        if dify_config.RBAC_ENABLED is not False or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY:
            raise _CoordinationConflict("local_mode_required")
        self.caller._leases.ensure_owned()
        self.operation._check()

    def prelock(self, session):
        if self.root is not None and self.root.is_active:
            raise _CoordinationConflict("operation_root_reused")
        self.root = session.get_transaction()
        CasdoorInvitedLoginScopeRepository(
            session, self.caller._base._configuration_service._repository
        ).prelock_unconsumed_invitation(self.expected_scope, self.observation, remote_email=self.remote_email)

    def _current(self, session):
        a = self.discovered.attempt
        return CasdoorInvitedLoginScopeRepository(
            session, self.caller._base._configuration_service._repository
        ).discover_unconsumed_invitation(a.context, a.key, self.observation, remote_email=self.remote_email)

    def recheck(self, session, pending):
        """Closed account/identity/intent delta; no policy DTO is trusted here."""
        if session.get_transaction() is not self.root:
            raise _CoordinationConflict("operation_root_changed")
        before, after = self.discovered, self._current(session)
        b, a = before.scope, after.scope
        if (
            after.attempt != before.attempt
            or after.credentials_sha256 != before.credentials_sha256
            or after.legacy_links_sha256 != before.legacy_links_sha256
            or after.issuance != before.issuance
            or after.lifecycle != before.lifecycle
            or a.configuration != b.configuration
            or a.namespaces != b.namespaces
            or a.workspaces != b.workspaces
            or a.joins != b.joins
            or a.histories != b.histories
            or a.lease_scope != b.lease_scope
            or len(a.identities) != 1
            or (b.identities and a.identities != b.identities)
            or len(a.intents) != 1
            or a.intents[0].id != str(pending.operation_id)
            or a.account.status is not AccountStatus.ACTIVE
            or a.account.initialized_at is None
        ):
            raise _CoordinationConflict("operation_postwrite_drift")
        # Reconstruct with only the original initialization owner's assignments
        # restored; initialized accounts preserve every profile field/timestamp.
        expected = dict(b.account._mapping)
        expected["status"] = AccountStatus.ACTIVE
        if b.account.initialized_at is None:
            setup = self.prepared.plan.setup
            if setup is None:
                raise _CoordinationConflict("operation_setup_missing")
            expected.update(
                name=setup.name,
                interface_language=setup.interface_language,
                timezone=setup.timezone,
                interface_theme="light",
                initialized_at=a.account.initialized_at,
            )
        if dict(a.account._mapping) != expected:
            raise _CoordinationConflict("operation_account_drift")
        if before.quota:
            if after.quota != before.quota:
                raise _CoordinationConflict("operation_quota_drift")
        elif len(after.quota) != 1 or (
            after.quota[0].account_id != str(after.attempt.account_id)
            or after.quota[0].total_quota != dify_config.ACCOUNT_TOTAL_QUOTA
            or after.quota[0].used_quota != 0
        ):
            raise _CoordinationConflict("operation_quota_drift")
        return after

    def check_unchanged(self, session):
        if self._current(session) != self.expected_scope:
            raise _CoordinationConflict("operation_scope_drift")


def _operation_guard(value, *, owner, attempt, prepared):
    """Private production entry must consume the caller's original issued guard."""
    if (
        type(value) is not _InvitedOperationGuard
        or value not in _ISSUED_OPERATION_GUARDS
        or value.discovered.attempt != attempt
        or value.prepared is not prepared
        or value.caller._operation_owner is not owner
    ):
        raise _CoordinationConflict("invalid_operation_guard")
    _ISSUED_OPERATION_GUARDS.discard(value)
    return value


def _invited_scope(caller, session, attempt, remote_email):
    scope = CasdoorLoginScopeRepository(session, caller._base._configuration_service._repository)._project_scope(
        attempt.context,
        attempt.key,
        attempt.account_id,
        creation_email=None,
        extra_workspace_ids=(attempt.workspace_id,),
    )
    return replace(
        scope,
        lease_scope=replace(
            scope.lease_scope,
            emails=tuple(sorted(set(scope.lease_scope.emails) | {remote_email})),
        ),
    )


def _tail_rows(caller, session, attempt):
    """Bounded transient complete account lineage; never durable/public."""
    owner = CasdoorLoginScopeRepository(session, caller._base._configuration_service._repository)
    result = {}
    for name, model in (
        ("account", Account),
        ("identities", Identity),
        ("joins", TenantAccountJoin),
        ("histories", History),
        ("quota", AccountMoneyExtend),
        ("links", AccountIntegrate),
    ):
        column = model.id if model is Account else model.account_id
        result[name] = owner._rows(
            sa.select(*model.__table__.columns).where(column == str(attempt.account_id)).order_by(model.id),
            cap=1 if model in (Account, AccountMoneyExtend) else 2048,
        )
    if len(result["account"]) != 1 or len(result["quota"]) != 1:
        raise _CoordinationConflict("invited_account_lineage_missing")
    return result


class _InvitedContinuation:
    """Registered only by the actual signed A stack, never by a public result."""

    def __init__(
        self,
        guard,
        committed,
        bundle,
        profile,
        roles,
        configuration,
        correlation,
        ip_address,
    ):
        self.guard, self.committed = guard, committed
        self.bundle, self.profile, self.roles, self.configuration = (
            bundle,
            profile,
            roles,
            configuration,
        )
        self.correlation, self.ip_address = correlation, ip_address


@dataclass(frozen=True, eq=False, repr=False)
class _InvitedRecoveryContinuation:
    """Only the freshly authenticated caller registers this single-use lineage."""

    caller: object
    consumed: object
    operation: object
    candidate: object
    bundle: object
    profile: object
    online: object
    roles: object
    configuration: object
    preflight: object
    plan: object
    leases: object
    correlation: object
    ip_address: object


@dataclass(frozen=True, eq=False, repr=False)
class _InvitedSessionTail:
    """Single-use private tail registered by an actual first/recovery stack."""

    caller: object
    attempt: object
    snapshot: object
    consumed: object
    operation: object
    plan: object
    remote_email: str
    profile: object
    roles: object
    configuration: object
    correlation: object
    ip_address: object
    identity_id: UUID
    generation: int
    leases: object
    ensure: object
    recovery: object = None
    recovery_candidate: object = None
    recovery_rows: object = None


class _InvitedFinalizationGuard:
    def __init__(self, continuation, owner):
        self.continuation, self.owner = continuation, owner
        self.upstream = continuation.guard
        self.caller = self.upstream.caller
        self.attempt = self.upstream.discovered.attempt
        self.deadline = self.upstream.operation.deadline
        self.expected = self.upstream.expected_scope.scope
        self.rows = None
        self.root = None

    def ensure(self):
        self.upstream.ensure()

    def current(self, session):
        return _invited_scope(self.caller, session, self.attempt, self.upstream.remote_email)

    def prelock(self, session):
        root = session.get_transaction()
        if root is None or getattr(root, "_connections", None) != {}:
            raise _CoordinationConflict("invited_root_not_first")
        self.root = root
        self.check(session)
        owner = CasdoorLoginScopeRepository(session, self.caller._base._configuration_service._repository)
        owner._lock_invited_integration(self.attempt.context)
        self.check(session)
        owner._lock_invited_candidate_parents(self.attempt.context, self.expected)
        CasdoorInvitationFinalizationRepository(session).inspect(self.attempt)
        self.check(session)

    def check(self, session):
        if session.get_transaction() is not self.root or self.current(session) != self.expected:
            raise _CoordinationConflict("invited_finalization_drift")
        rows = _tail_rows(self.caller, session, self.attempt)
        if self.rows is None:
            self.rows = rows
        elif rows != self.rows:
            raise _CoordinationConflict("invited_finalization_lineage_drift")

    def accept(self, session, completed):
        before, after = self.expected, self.current(session)
        facts = completed
        if (
            not facts.completed
            or facts.snapshot != self.continuation.committed.snapshot
            or replace(after, joins=before.joins, intents=before.intents) != before
            or len(after.intents) != 1
            or after.intents[0].id != str(facts.snapshot.operation_id)
            or after.intents[0].operation_state is not CasdoorOperationState.APPLIED
            or after.intents[0].termination_state is not CasdoorTerminationState.CONFIRMED
        ):
            raise _CoordinationConflict("invited_completion_delta")
        prior = {row.id: row for row in before.joins}
        current = {row.id: row for row in after.joins}
        if any(current.pop(key, None) != row for key, row in prior.items()):
            raise _CoordinationConflict("invited_existing_join_delta")
        if current and (
            len(current) != 1
            or next(iter(current)) != facts.join_id
            or next(iter(current.values())).tenant_id != str(self.attempt.workspace_id)
        ):
            raise _CoordinationConflict("invited_new_join_delta")
        rows = _tail_rows(self.caller, session, self.attempt)
        if any(rows[k] != self.rows[k] for k in self.rows if k != "joins"):
            raise _CoordinationConflict("invited_completion_lineage_delta")
        old = {row.id: row for row in self.rows["joins"]}
        new = {row.id: row for row in rows["joins"]}
        if any(new.pop(key, None) != row for key, row in old.items()):
            raise _CoordinationConflict("invited_full_join_delta")
        if new and (len(new) != 1 or next(iter(new)) != facts.join_id):
            raise _CoordinationConflict("invited_full_join_delta")
        self.expected, self.rows = after, rows


def _finalization_guard(value, *, owner, attempt):
    if (
        type(value) is not _InvitedFinalizationGuard
        or value not in _ISSUED_FINALIZATION_GUARDS
        or value.owner is not owner
        or value.attempt != attempt
        or value.caller._invitation_finalizer is not owner
    ):
        raise _CoordinationConflict("invalid_invited_finalization_guard")
    _ISSUED_FINALIZATION_GUARDS.discard(value)
    return value


def _resume_same_rows(before, after, *, changed=()):
    """Compare complete scalar rows; permitted columns are owner effects only."""
    if len(before) != len(after):
        return False
    return all(
        {k: v for k, v in old._mapping.items() if k not in changed}
        == {k: v for k, v in new._mapping.items() if k not in changed}
        for old, new in zip(before, after, strict=True)
    )


class _InvitedRecoveryFinalizationGuard:
    """Only the actual resumed native stack registers this P3L root guard."""

    def __init__(self, continuation, owner):
        self.continuation, self.owner = continuation, owner
        self.caller = continuation.caller
        self.attempt = continuation.candidate.attempt
        self.deadline = continuation.operation.deadline
        self.expected = continuation.candidate
        self.root, self.rows = None, None

    def ensure(self):
        self.caller._check_recovery(self.continuation)

    def prelock(self, session):
        root = session.get_transaction()
        if root is None or getattr(root, "_connections", None) != {}:
            raise _CoordinationConflict("invitation_resume_root_not_first")
        self.ensure()
        self.root = root
        owner = CasdoorLoginScopeRepository(session, self.caller._base._configuration_service._repository)
        owner._lock_invited_integration(self.attempt.context)
        self.ensure()
        owner._lock_invited_candidate_parents(self.attempt.context, self.expected.scope)
        self.check(session)

    def check(self, session):
        self.ensure()
        if session.get_transaction() is not self.root:
            raise _CoordinationConflict("invitation_resume_root_drift")
        current, rows = self.caller._resume_observation(session, self.continuation)
        if current != self.expected or (self.rows is not None and rows != self.rows):
            raise _CoordinationConflict("invitation_resume_finalization_drift")
        self.rows = rows

    def accept(self, session, completed):
        self.ensure()
        before, rows = self.expected, self.rows
        after, fresh = self.caller._resume_observation(session, self.continuation)
        if (
            session.get_transaction() is not self.root
            or rows is None
            or before.phase != "pending"
            or after.phase != "invitation_completed"
            or not completed.completed
            or completed.snapshot != before.snapshot
            or after.attempt != before.attempt
            or after.snapshot != before.snapshot
            or replace(after.scope, joins=before.scope.joins, intents=before.scope.intents) != before.scope
            or after.credentials_sha256 != before.credentials_sha256
            or after.legacy_links_sha256 != before.legacy_links_sha256
            or after.quota != before.quota
            or len(after.scope.intents) != 1
            or next(row for row in fresh["intents"] if row.id == str(before.snapshot.operation_id)).proof_ref
            != completed.proof_ref
        ):
            raise _CoordinationConflict("invitation_resume_completion_delta")
        changes = {
            "intents": (
                "operation_state",
                "termination_state",
                "terminated_at",
                "updated_at",
                "termination_proof_kind",
                "proof_ref",
            ),
            "issuances": ("state", "consumption_receipt_json", "consumed_at", "updated_at"),
            "lifecycles": ("epoch", "updated_at"),
        }
        for name in rows:
            if name == "joins":
                continue
            if name in changes:
                field, target = {
                    "intents": ("id", str(before.snapshot.operation_id)),
                    "issuances": ("issuance_id", str(self.attempt.issuance_id)),
                    "lifecycles": ("lifecycle_id", before.lifecycle[0].lifecycle_id),
                }[name]
                old_target = tuple(row for row in rows[name] if getattr(row, field) == target)
                new_target = tuple(row for row in fresh[name] if getattr(row, field) == target)
                preserved = tuple(row for row in rows[name] if getattr(row, field) != target) == tuple(
                    row for row in fresh[name] if getattr(row, field) != target
                )
                valid = (
                    len(old_target) == len(new_target) == 1
                    and preserved
                    and _resume_same_rows(
                        old_target,
                        new_target,
                        changed=changes[name],
                    )
                )
            else:
                valid = rows[name] == fresh[name]
            if not valid:
                raise _CoordinationConflict("invitation_resume_completion_lineage_delta")
        prior = {row.id: row for row in rows["joins"]}
        new = {row.id: row for row in fresh["joins"]}
        if any(new.pop(key, None) != value for key, value in prior.items()):
            raise _CoordinationConflict("invitation_resume_existing_join_delta")
        if completed.membership_created:
            if len(new) != 1 or completed.join_id not in new:
                raise _CoordinationConflict("invitation_resume_new_join_delta")
            join = new[completed.join_id]
            # These are the unchanged shared helper/model's actual insert fields;
            # server timestamp values are read back, never fabricated.
            if (
                join.account_id != str(self.attempt.account_id)
                or join.tenant_id != str(self.attempt.workspace_id)
                or join.current is not False
                or join.invited_by is not None
                or join.last_opened_at is not None
                or join.created_at != join.updated_at
                or type(join.created_at) is not datetime
                or join.created_at.tzinfo is not None
                or join.created_at < before.snapshot.created_at.replace(microsecond=0)
                or join.created_at
                > next(row for row in fresh["intents"] if row.id == str(before.snapshot.operation_id)).terminated_at
            ):
                raise _CoordinationConflict("invitation_resume_new_join_fields")
        elif new:
            raise _CoordinationConflict("invitation_resume_unexpected_join")
        issuance = next(row for row in fresh["issuances"] if row.issuance_id == str(self.attempt.issuance_id))
        lifecycle = next(row for row in fresh["lifecycles"] if row.lifecycle_id == before.lifecycle[0].lifecycle_id)
        intent = next(row for row in fresh["intents"] if row.id == str(before.snapshot.operation_id))
        if (
            issuance.consumed_at != issuance.updated_at
            or lifecycle.epoch != completed.result_epoch
            or completed.result_epoch != before.lifecycle[0].epoch + int(completed.membership_created)
            or (
                not completed.membership_created
                and next(row for row in rows["lifecycles"] if row.lifecycle_id == lifecycle.lifecycle_id) != lifecycle
            )
            or lifecycle.updated_at < before.lifecycle[0].updated_at
            or lifecycle.updated_at > intent.terminated_at
            or intent.terminated_at != intent.updated_at
        ):
            raise _CoordinationConflict("invitation_resume_authority_delta")
        self.expected, self.rows = after, fresh


def _recovery_finalization_guard(value, *, owner, attempt):
    if (
        type(value) is not _InvitedRecoveryFinalizationGuard
        or value not in _ISSUED_RECOVERY_FINALIZATION_GUARDS
        or value.owner is not owner
        or value.attempt != attempt
        or value.caller._invitation_finalizer is not owner
    ):
        raise _CoordinationConflict("invalid_invitation_resume_guard")
    _ISSUED_RECOVERY_FINALIZATION_GUARDS.discard(value)
    return value


class CasdoorInvitedLocalLoginCoordinatorService:
    def __init__(
        self,
        *,
        ordinary: CasdoorLocalLoginCoordinatorService,
        activation: AccountActivationService,
    ):
        if (
            type(ordinary) is not CasdoorLocalLoginCoordinatorService
            or type(activation) is not AccountActivationService
        ):
            raise _CoordinationConflict("invalid_invited_owner")
        if type(activation._tokens) is not RedisInvitationTokenStore:
            raise _CoordinationConflict("versioned_invitation_store_required")
        self._base, self._activation = ordinary, activation
        self._store = activation._tokens
        self._invited_account = CasdoorInvitedLoginAccountService(activation=activation)
        self._operation_owner = CasdoorInvitationOperationService(
            session_factory=ordinary._session_factory,
            store=self._store,
            invited_login=self._invited_account,
        )
        self._leases = None
        self._invitation_finalizer = CasdoorInvitationFinalizationService(
            session_factory=ordinary._session_factory,
            store=self._store,
        )
        self._session_gateway = ordinary._finalization._session_gateway if ordinary._finalization is not None else None

    @classmethod
    def for_production(
        cls,
        *,
        session_factory,
        configuration_service,
        account_activation,
        redis_client,
        deployment_policy_service=None,
    ):
        ordinary = CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=session_factory,
            configuration_service=configuration_service,
            account_owner=CasdoorLoginAccountService(activation=account_activation),
            redis_client=redis_client,
            deployment_policy_service=deployment_policy_service,
        )
        return cls(ordinary=ordinary, activation=account_activation)

    @property
    def _production_policy(self):
        return self._base._production_policy

    def _with_request_redis(self, redis_client):
        activation = copy(self._activation)
        activation._tokens = RedisInvitationTokenStore(redis=redis_client)
        return type(self)(ordinary=self._base._with_request_redis(redis_client), activation=activation)

    def _coordinate_local_login(self, **kwargs):
        consumed = kwargs.get("consumed")
        if type(consumed) is ConsumedAuthTransaction and consumed.context.invite is None:
            return self._base._coordinate_local_login(**kwargs)
        return self._coordinate_invited_local_login(**kwargs)

    def _discover(self, context, key, observation, profile):
        with self._base._session_factory() as session, session.begin():
            return CasdoorInvitedLoginScopeRepository(
                session, self._base._configuration_service._repository
            ).discover_unconsumed_invitation(context, key, observation, remote_email=profile.email)

    def _admission(self, consumed, bundle, online, profile, discovered):
        attempt = discovered.attempt
        with self._base._session_factory() as session, session.begin():
            preflight = CasdoorAccountPreflightRepository(session).reconstruct(
                attempt.context,
                attempt.key,
                candidate_account_id=attempt.account_id,
                collision_email=profile.email,
            )
            invitation = InvitationObservation(
                attempt.context.namespace_id,
                attempt.context.subject,
                attempt.account_id,
                discovered.scope.account.email,
                attempt.workspace_id,
            )
            plan = decide_admission(
                context=attempt.context,
                mode=AuthMode.LOGIN,
                tokens=bundle,
                online=online,
                profile=profile,
                account=preflight.account,
                binding=preflight.exact_binding,
                invitation=invitation,
                email_collision_account_ids=preflight.collision_account_ids,
                request_language=consumed.context.locale,
                request_timezone=consumed.context.timezone,
            )
        return preflight, invitation, plan

    def _discover_recovery(self, context, key, consumed, profile):
        with self._base._session_factory() as session, session.begin():
            return CasdoorInvitedLoginScopeRepository(
                session, self._base._configuration_service._repository
            )._discover_invitation_recovery(
                context,
                key,
                token_digest=sha256(consumed.context.invite.encode()).hexdigest(),
                remote_email=profile.email,
            )

    def _recovery_admission(self, consumed, bundle, online, profile, candidate, session=None):
        if session is None:
            with self._base._session_factory() as current, current.begin():
                return self._recovery_admission(consumed, bundle, online, profile, candidate, session=current)
        attempt = candidate.attempt
        preflight = CasdoorAccountPreflightRepository(session).reconstruct(
            attempt.context,
            attempt.key,
            candidate_account_id=attempt.account_id,
            collision_email=profile.email,
        )
        plan = decide_admission(
            context=attempt.context,
            mode=AuthMode.LOGIN,
            tokens=bundle,
            online=online,
            profile=profile,
            account=preflight.account,
            binding=preflight.exact_binding,
            invitation=None,
            source=None,
            email_collision_account_ids=preflight.collision_account_ids,
            request_language=consumed.context.locale,
            request_timezone=consumed.context.timezone,
        )
        if (
            plan.action is not AdmissionAction.USE_BOUND
            or plan.account_id != attempt.account_id
            or plan.setup is not None
            or plan.required_shared_owners != ()
            or preflight.account.status is not AccountStatus.ACTIVE
            or preflight.account.initialized_at is None
        ):
            raise _CoordinationConflict("invitation_recovery_admission")
        return preflight, plan

    def _check_recovery(self, continuation):
        if (
            type(continuation) is not _InvitedRecoveryContinuation
            or continuation.caller is not self
            or continuation.leases is not self._leases
        ):
            raise _CoordinationConflict("invalid_invitation_recovery_lineage")
        continuation.operation._check()
        self._base._require_production_policy()
        if dify_config.RBAC_ENABLED is not False or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY:
            raise _CoordinationConflict("local_mode_required")
        continuation.leases.ensure_owned()
        continuation.operation._check()

    def _recovery_barrier(self, session, continuation, expected):
        self._check_recovery(continuation)
        attempt = expected.attempt
        current = CasdoorInvitedLoginScopeRepository(
            session, self._base._configuration_service._repository
        )._discover_invitation_recovery(
            attempt.context,
            attempt.key,
            token_digest=sha256(continuation.consumed.context.invite.encode()).hexdigest(),
            remote_email=continuation.profile.email,
        )
        if (
            current != expected
            or current.phase != "finalized"
            or current.scope.configuration != continuation.configuration
            or current.scope.lease_scope.canonical_keys != continuation.leases.canonical_keys
            or self._recovery_admission(
                continuation.consumed,
                continuation.bundle,
                continuation.online,
                continuation.profile,
                current,
                session=session,
            )
            != (continuation.preflight, continuation.plan)
        ):
            raise _CoordinationConflict("invitation_recovery_scope_drift")
        return current

    def _recovery_quota_rows(self, session, attempt):
        owner = CasdoorLoginScopeRepository(session, self._base._configuration_service._repository)
        result = {}
        for name, model in (
            ("account", Account),
            ("identities", Identity),
            ("joins", TenantAccountJoin),
            ("histories", History),
            ("quota", AccountMoneyExtend),
            ("links", AccountIntegrate),
            ("intents", Intent),
            ("audits", Audit),
            ("issuances", Issuance),
            ("lifecycles", Lifecycle),
        ):
            column = model.id if model is Account else model.account_id
            if model in (Intent, Audit, Issuance, Lifecycle):
                result[name] = CasdoorInvitedLoginScopeRepository(
                    session, self._base._configuration_service._repository
                )._recovery_rows(model, column == str(attempt.account_id), cap=2048, text_limit=32768)
            else:
                result[name] = owner._rows(
                    sa.select(*model.__table__.columns).where(column == str(attempt.account_id)).order_by(model.id),
                    cap=1 if model in (Account, AccountMoneyExtend) else 2048,
                )
        if len(result["account"]) != 1:
            raise _CoordinationConflict("invitation_recovery_account_missing")
        return result

    def _ensure_recovery_quota(self, continuation, tail):
        attempt = tail.attempt
        with self._base._session_factory() as session, session.begin():
            owner, observed, _ = self._receipt_barrier(session, tail)
            expected_candidate = self._recovery_barrier(session, continuation, continuation.candidate)
            before = self._recovery_quota_rows(session, attempt)
            self._check_recovery(continuation)
            self._receipt_barrier(session, tail, owner, observed)
            if self._recovery_quota_rows(session, attempt) != before:
                raise _CoordinationConflict("invitation_recovery_quota_prewrite_drift")
            if not before["quota"]:
                from services.account_quota_service_extend import ensure_account_quota_extend

                ensure_account_quota_extend(str(attempt.account_id), session=session)
                session.flush()
            expected = self._recovery_quota_rows(session, attempt)
            if (
                len(expected["quota"]) != 1
                or any(expected[k] != before[k] for k in before if k != "quota")
                or (before["quota"] and expected["quota"] != before["quota"])
                or (
                    not before["quota"]
                    and (
                        expected["quota"][0].account_id != str(attempt.account_id)
                        or expected["quota"][0].total_quota != dify_config.ACCOUNT_TOTAL_QUOTA
                        or expected["quota"][0].used_quota != 0
                        or type(UUID(expected["quota"][0].id)) is not UUID
                    )
                )
            ):
                raise _CoordinationConflict("invitation_recovery_quota_delta")
            expected_candidate = replace(expected_candidate, quota=expected["quota"])
            self._recovery_barrier(session, continuation, expected_candidate)
            self._receipt_barrier(session, tail, owner, observed)
            self._check_recovery(continuation)
            if self._recovery_quota_rows(session, attempt) != expected:
                raise _CoordinationConflict("invitation_recovery_quota_postwrite_drift")
        self._check_recovery(continuation)
        with self._base._session_factory() as session, session.begin():
            owner, current, _ = self._receipt_barrier(session, tail)
            self._recovery_barrier(session, continuation, expected_candidate)
            if current != observed or self._recovery_quota_rows(session, attempt) != expected:
                raise _CoordinationConflict("invitation_recovery_quota_readback_drift")
            self._check_recovery(continuation)
            self._receipt_barrier(session, tail, owner, current)
            if self._recovery_quota_rows(session, attempt) != expected:
                raise _CoordinationConflict("invitation_recovery_quota_readback_drift")
        return expected_candidate, expected

    def _continue_invited_recovery(self, continuation):
        if (
            type(continuation) is not _InvitedRecoveryContinuation
            or continuation not in _ISSUED_RECOVERY_CONTINUATIONS
            or continuation.caller is not self
            or type(self._session_gateway) is not RedisAccountSessionGateway
        ):
            raise _CoordinationConflict("invalid_invitation_recovery_continuation")
        _ISSUED_RECOVERY_CONTINUATIONS.discard(continuation)
        try:
            parse_ip_address(continuation.ip_address)
            self._check_recovery(continuation)
            candidate = continuation.candidate
            with self._base._session_factory() as session, session.begin():
                owner = CasdoorInvitedFinalizationReceiptRepository(
                    session, self._base._configuration_service._repository
                )
                observed = owner.observe(candidate.attempt, roles=continuation.roles)
                current = self._recovery_barrier(session, continuation, candidate)
                identity = current.scope.identities[0]
                if observed.current_generation != candidate.attempt.expected_generation + 1:
                    raise _CoordinationConflict("invitation_recovery_generation")
            tail = _InvitedSessionTail(
                self,
                candidate.attempt,
                candidate.snapshot,
                continuation.consumed,
                continuation.operation,
                continuation.plan,
                continuation.profile.email,
                continuation.profile,
                continuation.roles,
                continuation.configuration,
                continuation.correlation,
                continuation.ip_address,
                UUID(identity.id),
                observed.current_generation,
                continuation.leases,
                lambda: self._check_recovery(continuation),
            )
            quota_candidate, quota_rows = self._ensure_recovery_quota(continuation, tail)
            tail = replace(
                tail,
                recovery=continuation,
                recovery_candidate=quota_candidate,
                recovery_rows=quota_rows,
            )
            _ISSUED_SESSION_TAILS.add(tail)
            return self._finish_invited_session(tail)
        except BaseException as error:
            if not hasattr(error, "finalization_outcome"):
                error.finalization_outcome, error.token_outcome = "unknown", "not_started"
            raise

    def _resume_observation(self, session, continuation):
        self._check_recovery(continuation)
        attempt = continuation.candidate.attempt
        candidate = CasdoorInvitedLoginScopeRepository(
            session, self._base._configuration_service._repository
        )._discover_invitation_recovery(
            attempt.context,
            attempt.key,
            token_digest=sha256(continuation.consumed.context.invite.encode()).hexdigest(),
            remote_email=continuation.profile.email,
        )
        if (
            candidate is None
            or candidate.attempt != attempt
            or candidate.snapshot != continuation.candidate.snapshot
            or candidate.scope.configuration != continuation.configuration
            or candidate.scope.lease_scope.canonical_keys != continuation.leases.canonical_keys
        ):
            raise _CoordinationConflict("invitation_resume_scope_drift")
        self._recovery_admission(
            continuation.consumed,
            continuation.bundle,
            continuation.online,
            continuation.profile,
            candidate,
            session=session,
        )
        rows = self._recovery_quota_rows(session, attempt)
        self._check_recovery(continuation)
        return candidate, rows

    def _resume_read(self, continuation, *, phase):
        self._check_recovery(continuation)
        with self._base._session_factory() as session, session.begin():
            attempt = continuation.candidate.attempt
            if phase == "memberships_written":
                observed = CasdoorInvitedWriteReceiptRepository(
                    session, self._base._configuration_service._repository
                ).observe(attempt)
                if observed.current_generation != attempt.expected_generation + 1:
                    raise _CoordinationConflict("invitation_resume_write_generation")
            elif phase == "finalized":
                CasdoorInvitedFinalizationReceiptRepository(
                    session, self._base._configuration_service._repository
                ).observe(attempt, roles=continuation.roles)
            else:
                guard = _InvitedRecoveryFinalizationGuard(continuation, self._invitation_finalizer)
                guard.prelock(session)
                facts = CasdoorInvitationFinalizationRepository(session).inspect(attempt)
                if facts.completed != (phase == "invitation_completed"):
                    raise _CoordinationConflict("invitation_resume_completion_phase")
                guard.check(session)
            current, rows = self._resume_observation(session, continuation)
            if current.phase != phase:
                raise _CoordinationConflict("invitation_resume_phase_drift")
            return current, rows

    def _resume_membership_delta(self, before, after, *, finalizing):
        old, rows = before
        new, fresh = after
        allowed = (
            {"histories", "audits"} if finalizing else {"identities", "joins", "histories", "audits", "lifecycles"}
        )
        if (
            old.attempt != new.attempt
            or old.snapshot != new.snapshot
            or old.credentials_sha256 != new.credentials_sha256
            or old.legacy_links_sha256 != new.legacy_links_sha256
            or any(rows[name] != fresh[name] for name in rows if name not in allowed)
            or any(row not in fresh["audits"] for row in rows["audits"])
        ):
            raise _CoordinationConflict("invitation_resume_membership_lineage_delta")
        from repositories.casdoor_invited_recovery_delta_repository_extend import verify_recovery_membership_delta

        verify_recovery_membership_delta(before, after, finalizing=finalizing)
        if finalizing:
            if not _resume_same_rows(rows["histories"], fresh["histories"], changed=("finalization", "updated_at")):
                raise _CoordinationConflict("invitation_resume_history_finalization_delta")
        else:
            target_id = next(
                row.id for row in rows["identities"] if row.namespace_id == str(old.attempt.context.namespace_id)
            )
            original_identity = tuple(row for row in rows["identities"] if row.id == target_id)
            current_identity = tuple(row for row in fresh["identities"] if row.id == target_id)
            if not _resume_same_rows(
                original_identity, current_identity, changed=("sync_generation", "updated_at")
            ) or tuple(row for row in rows["identities"] if row.id != target_id) != tuple(
                row for row in fresh["identities"] if row.id != target_id
            ):
                raise _CoordinationConflict("invitation_resume_identity_delta")

    def _continue_invited_resume(self, continuation):
        if (
            type(continuation) is not _InvitedRecoveryContinuation
            or continuation not in _ISSUED_RECOVERY_CONTINUATIONS
            or continuation.caller is not self
            or type(self._session_gateway) is not RedisAccountSessionGateway
        ):
            raise _CoordinationConflict("invalid_invitation_resume_continuation")
        _ISSUED_RECOVERY_CONTINUATIONS.discard(continuation)
        try:
            parse_ip_address(continuation.ip_address)
            current, rows = self._resume_read(continuation, phase=continuation.candidate.phase)
            if current != continuation.candidate:
                raise _CoordinationConflict("invitation_resume_candidate_drift")
            if current.phase == "pending":
                guard = _InvitedRecoveryFinalizationGuard(continuation, self._invitation_finalizer)
                _ISSUED_RECOVERY_FINALIZATION_GUARDS.add(guard)
                self._invitation_finalizer._finalize_invited_recovery(
                    current.attempt,
                    token=continuation.consumed.context.invite,
                    caller_guard=guard,
                )
                # This replacement follows the original owner's acknowledged
                # completion/readback, and carries no new authentication.
                continuation = replace(continuation, candidate=guard.expected)
                current, rows = self._resume_read(continuation, phase="invitation_completed")
            if current.phase == "invitation_completed":
                before = current, rows
                CasdoorInvitedLocalMembershipService(
                    session_factory=self._base._session_factory,
                    configuration_factory=self._base._configuration_service._repository,
                ).persist_invited_local_memberships(
                    current.attempt,
                    roles=continuation.roles,
                    leases=continuation.leases,
                    deadline=continuation.operation.deadline,
                )
                current, rows = self._resume_read(continuation, phase="memberships_written")
                self._resume_membership_delta(before, (current, rows), finalizing=False)
                continuation = replace(continuation, candidate=current)
            if current.phase == "memberships_written":
                before = current, rows
                CasdoorInvitedLocalFinalizationService(
                    session_factory=self._base._session_factory,
                    configuration_factory=self._base._configuration_service._repository,
                ).finalize_invited_local_memberships(
                    current.attempt,
                    roles=continuation.roles,
                    leases=continuation.leases,
                    deadline=continuation.operation.deadline,
                )
                current, rows = self._resume_read(continuation, phase="finalized")
                self._resume_membership_delta(before, (current, rows), finalizing=True)
            if current.phase != "finalized":
                raise _CoordinationConflict("invitation_resume_incomplete")
            self._check_recovery(continuation)
            preflight, plan = self._recovery_admission(
                continuation.consumed,
                continuation.bundle,
                continuation.online,
                continuation.profile,
                current,
            )
            bridge = replace(continuation, candidate=current, preflight=preflight, plan=plan)
            _ISSUED_RECOVERY_CONTINUATIONS.add(bridge)
            return self._continue_invited_recovery(bridge)
        except BaseException as error:
            if not hasattr(error, "finalization_outcome"):
                error.finalization_outcome, error.token_outcome = "unknown", "not_started"
            raise

    def _receipt_barrier(self, session, continuation, owner=None, observed=None):
        """Actual F2 first SQL; repeat its private read owner after the last I/O."""
        attempt = continuation.attempt
        if owner is None:
            owner = CasdoorInvitedFinalizationReceiptRepository(session, self._base._configuration_service._repository)
            observed = owner.observe(attempt, roles=continuation.roles)
        scope = _invited_scope(self, session, attempt, continuation.remote_email)
        if (
            scope.configuration != continuation.configuration
            or scope.lease_scope.canonical_keys != self._leases.canonical_keys
            or len(scope.intents) != 1
            or scope.intents[0].id != str(continuation.snapshot.operation_id)
            or scope.intents[0].operation_state is not CasdoorOperationState.APPLIED
            or scope.intents[0].termination_state is not CasdoorTerminationState.CONFIRMED
            or observed.operation_id != continuation.snapshot.operation_id
            or observed.current_generation != attempt.expected_generation + 1
        ):
            raise _CoordinationConflict("invited_required_intent_barrier")
        reader = owner._reader
        rows = reader._rows(attempt, lock=False)
        audit = owner._f_row(rows[3].id, lock=False)
        if owner._verify(attempt, continuation.roles, scope, rows, audit) != observed:
            raise _CoordinationConflict("invited_receipt_drift")
        return owner, observed, scope

    def _profile_delta(self, before, after, continuation, result, now):
        """Preserve every unrelated column while accepting original profile writes."""
        attempt = continuation.attempt
        generation = attempt.expected_generation + 1
        if (
            type(result) is not ProfilePersistenceOutcome
            or result.account_id != attempt.account_id
            or result.generation != generation
            or len(before["identities"]) != 1
            or len(after["identities"]) != 1
            or result.identity_id != UUID(after["identities"][0].id)
            or any(before[k] != after[k] for k in before if k not in ("account", "identities"))
        ):
            raise _CoordinationConflict("invited_profile_lineage_delta")
        old, current = before["account"][0], after["account"][0]
        expected_name = old.name
        if result.name_status is ProfileAuditResult.APPLIED:
            if result.name_reason is ProfileNameReason.FILLED_EMPTY and old.name.strip():
                raise _CoordinationConflict("invited_profile_name_delta")
            if (
                result.name_reason is ProfileNameReason.MANAGED_UPDATE
                and continuation.configuration.name_sync != "managed"
            ):
                raise _CoordinationConflict("invited_profile_name_delta")
            if result.name_reason not in (
                ProfileNameReason.FILLED_EMPTY,
                ProfileNameReason.MANAGED_UPDATE,
            ):
                raise _CoordinationConflict("invited_profile_name_delta")
            expected_name = continuation.profile.name
        expected = dict(old._mapping, name=expected_name, updated_at=current.updated_at)
        if dict(current._mapping) != expected:
            raise _CoordinationConflict("invited_profile_account_delta")
        prior, identity = before["identities"][0], after["identities"][0]
        if not result.snapshot_changed and after != before:
            raise _CoordinationConflict("invited_profile_unchanged_delta")
        allowed = {
            "last_applied_json",
            "profile_sync_json",
            "remote_profile_version",
            "remote_email",
            "email_verified",
            "last_seen_at",
            "updated_at",
        }
        if any(getattr(identity, k) != value for k, value in prior._mapping.items() if k not in allowed):
            raise _CoordinationConflict("invited_profile_identity_delta")
        baseline = _snapshot(identity.last_applied_json, baseline=True, generation=generation)
        if result.name_status is not ProfileAuditResult.APPLIED and baseline != _snapshot(
            prior.last_applied_json, baseline=True, generation=generation
        ):
            raise _CoordinationConflict("invited_profile_baseline_delta")
        sync = _snapshot(identity.profile_sync_json, baseline=False, generation=generation)
        if not sync or _parse_time(sync["last_sync_at"]) != result.synced_at:
            raise _CoordinationConflict("invited_profile_snapshot_delta")
        if result.name_status is ProfileAuditResult.APPLIED and (
            baseline.get("name") != continuation.profile.name or baseline.get("name_generation") != generation
        ):
            raise _CoordinationConflict("invited_profile_baseline_delta")
        if result.snapshot_changed and (
            sync["generation"] != generation
            or _parse_time(sync["auth_started_at"]) != continuation.consumed.auth_started_at
            or sync["correlation_id"] != str(continuation.correlation)
            or _parse_time(sync["last_sync_at"]) != now
            or sync["name_status"] != result.name_status.value
            or sync["name_reason"] != result.name_reason.value
            or sync["remote_email_status"] != result.remote_email_status.value
            or sync["remote_email_differs"] != result.remote_email_differs
            or identity.last_seen_at != now.replace(tzinfo=None)
            or identity.remote_email != validate_email(continuation.profile.email)
            or identity.email_verified != continuation.profile.email_verified
            or identity.remote_profile_version
            != "sha256:"
            + sha256(
                _dump(
                    {
                        "name": _name(continuation.profile.name),
                        "email": validate_email(continuation.profile.email),
                        "email_verified": continuation.profile.email_verified,
                    }
                ).encode("utf-8")
            ).hexdigest()
        ):
            raise _CoordinationConflict("invited_profile_snapshot_delta")

    def _continue_invited_login(self, continuation):
        """The actual A return alone reaches P3L/B3/F1/F2/C1 and the pure issuer."""
        if (
            type(continuation) is not _InvitedContinuation
            or continuation not in _ISSUED_CONTINUATIONS
            or continuation.guard.caller is not self
            or type(self._session_gateway) is not RedisAccountSessionGateway
        ):
            raise _CoordinationConflict("invalid_invited_continuation")
        _ISSUED_CONTINUATIONS.discard(continuation)
        g = continuation.guard
        attempt = g.discovered.attempt
        phase, tokens = "not_started", "not_started"
        try:
            parse_ip_address(continuation.ip_address)
            final_guard = _InvitedFinalizationGuard(continuation, self._invitation_finalizer)
            _ISSUED_FINALIZATION_GUARDS.add(final_guard)
            g.ensure()
            self._invitation_finalizer._finalize_invited_login(
                attempt,
                token=g.consumed.context.invite,
                caller_guard=final_guard,
            )
            g.ensure()
            configuration_factory = self._base._configuration_service._repository
            membership = CasdoorInvitedLocalMembershipService(
                session_factory=self._base._session_factory,
                configuration_factory=configuration_factory,
            ).persist_invited_local_memberships(
                attempt,
                roles=continuation.roles,
                leases=self._leases,
                deadline=g.operation.deadline,
            )
            g.ensure()
            phase = "unknown"
            CasdoorInvitedLocalFinalizationService(
                session_factory=self._base._session_factory,
                configuration_factory=configuration_factory,
            ).finalize_invited_local_memberships(
                attempt,
                roles=continuation.roles,
                leases=self._leases,
                deadline=g.operation.deadline,
            )
            g.ensure()
            tail = _InvitedSessionTail(
                self,
                attempt,
                continuation.committed.snapshot,
                g.consumed,
                g.operation,
                g.prepared.plan,
                g.remote_email,
                continuation.profile,
                continuation.roles,
                continuation.configuration,
                continuation.correlation,
                continuation.ip_address,
                membership.identity_id,
                membership.generation,
                self._leases,
                g.ensure,
            )
            _ISSUED_SESSION_TAILS.add(tail)
            return self._finish_invited_session(tail)
        except BaseException as error:
            if not hasattr(error, "finalization_outcome"):
                error.finalization_outcome, error.token_outcome = phase, tokens
            raise

    def _finish_invited_session(self, continuation):
        if (
            type(continuation) is not _InvitedSessionTail
            or continuation not in _ISSUED_SESSION_TAILS
            or continuation.caller is not self
            or continuation.leases is not self._leases
            or continuation.generation != continuation.attempt.expected_generation + 1
            or type(self._session_gateway) is not RedisAccountSessionGateway
        ):
            raise _CoordinationConflict("invalid_invited_session_tail")
        _ISSUED_SESSION_TAILS.discard(continuation)
        attempt = continuation.attempt
        configuration_factory = self._base._configuration_service._repository
        phase, tokens = "unknown", "not_started"
        try:
            context = attempt.context
            mapping = MappingIdentityContext(
                context.integration_id,
                context.revision_id,
                context.namespace_id,
                continuation.identity_id,
                attempt.account_id,
                context.config_digest,
                context.issuer,
                context.organization,
                context.application,
                context.client_id,
                context.subject,
            )
            with self._base._session_factory() as session, session.begin():
                owner, observed, scope = self._receipt_barrier(session, continuation)
                if continuation.recovery is not None:
                    self._recovery_barrier(session, continuation.recovery, continuation.recovery_candidate)
                    if self._recovery_quota_rows(session, attempt) != continuation.recovery_rows:
                        raise _CoordinationConflict("invitation_recovery_tail_entry_drift")
                before = _tail_rows(self, session, attempt)
                continuation.ensure()
                self._receipt_barrier(session, continuation, owner, observed)
                if _tail_rows(self, session, attempt) != before:
                    raise _CoordinationConflict("invited_profile_prewrite_drift")
                now = datetime.now(UTC)
                profile = CasdoorProfileRepository(
                    session,
                    configuration_repository=configuration_factory(session),
                ).persist(
                    mapping,
                    continuation.profile,
                    expected_generation=continuation.generation,
                    expected_fence_epoch=context.fence_epoch,
                    auth_started_at=continuation.consumed.auth_started_at,
                    admission=continuation.plan,
                    correlation_id=continuation.correlation,
                    now=now,
                )
                session.flush()
                post_profile = _tail_rows(self, session, attempt)
                self._profile_delta(before, post_profile, continuation, profile, now)
                continuation.ensure()
                if _tail_rows(self, session, attempt) != post_profile:
                    raise _CoordinationConflict("invited_profile_postwrite_drift")
                CasdoorAvatarRepository(
                    session,
                    configuration_repository=configuration_factory(session),
                ).persist_pending(
                    mapping,
                    continuation.profile,
                    profile,
                    expected_generation=continuation.generation,
                    expected_fence_epoch=context.fence_epoch,
                    auth_started_at=continuation.consumed.auth_started_at,
                    correlation_id=continuation.correlation,
                    now=now,
                )
                session.flush()
                if _tail_rows(self, session, attempt) != post_profile:
                    raise _CoordinationConflict("invited_avatar_lineage_delta")
                # Original metadata owner queries relationships. Suppress its
                # autoflush so assignments are captured before SQL triggers.
                with session.no_autoflush:
                    selected = CasdoorLocalLoginFinalizationService._login_metadata(
                        session,
                        attempt.account_id,
                        continuation.ip_address,
                    )
                    expected_account = dict(post_profile["account"][0]._mapping)
                    actual_account = session.get(Account, str(attempt.account_id))
                    expected_account.update(
                        last_login_at=actual_account.last_login_at,
                        last_login_ip=actual_account.last_login_ip,
                    )
                    expected_joins = {row.id: dict(row._mapping) for row in post_profile["joins"]}
                    changed_joins = set()
                    for row in session.dirty:
                        if isinstance(row, TenantAccountJoin):
                            prior = expected_joins[row.id]
                            if (prior["current"], prior["last_opened_at"]) != (
                                row.current,
                                row.last_opened_at,
                            ):
                                changed_joins.add(row.id)
                            prior.update(current=row.current, last_opened_at=row.last_opened_at)
                session.flush()
                expected = _tail_rows(self, session, attempt)
                expected_account["updated_at"] = expected["account"][0].updated_at
                for row in expected["joins"]:
                    if row.id in changed_joins:
                        expected_joins[row.id]["updated_at"] = row.updated_at
                if (
                    dict(expected["account"][0]._mapping) != expected_account
                    or {row.id: dict(row._mapping) for row in expected["joins"]} != expected_joins
                    or any(expected[k] != post_profile[k] for k in expected if k not in ("account", "joins"))
                ):
                    raise _CoordinationConflict("invited_metadata_delta")
                owner, observed, expected_scope = self._receipt_barrier(session, continuation, owner, observed)
                continuation.ensure()
                self._receipt_barrier(session, continuation, owner, observed)
                if _tail_rows(self, session, attempt) != expected:
                    raise _CoordinationConflict("invited_metadata_postwrite_drift")
            phase = "committed"
            continuation.ensure()
            with self._base._session_factory() as session, session.begin():
                owner, current_observation, current_scope = self._receipt_barrier(session, continuation)
                if (
                    current_observation != observed
                    or current_scope != expected_scope
                    or _tail_rows(self, session, attempt) != expected
                ):
                    raise _CoordinationConflict("invited_final_readback_drift")
                join = session.get(TenantAccountJoin, selected)
                tenant = session.get(Tenant, join.tenant_id)
                if join.current is not True or tenant.status is not TenantStatus.NORMAL:
                    raise _CoordinationConflict("invited_current_tenant_drift")
                continuation.ensure()
                self._receipt_barrier(session, continuation, owner, observed)
                if _tail_rows(self, session, attempt) != expected:
                    raise _CoordinationConflict("invited_final_readback_drift")
            continuation.ensure()
            tokens = "unknown"
            pair = self._session_gateway.issue(str(attempt.account_id))
            tokens = "issued"
            continuation.ensure()
            return pair
        except BaseException as error:
            error.finalization_outcome, error.token_outcome = phase, tokens
            raise

    def _coordinate_invited_local_login(
        self,
        *,
        consumed,
        raw_tokens,
        operation,
        native_contract,
        directory_contract,
        credential_strategy,
        correlation_id,
        ip_address=None,
    ):
        if (
            type(consumed) is not ConsumedAuthTransaction
            or type(raw_tokens) is not RawTokens
            or type(operation) is not GatewayOperation
            or type(correlation_id) is not UUID
            or consumed.context.mode is not AuthMode.LOGIN
            or type(consumed.context.invite) is not str
            or any(
                v is not None
                for v in (
                    consumed.context.source,
                    consumed.context.identity_id,
                    consumed.context.action,
                )
            )
            or not operation._exchange_started
            or dify_config.RBAC_ENABLED is not False
            or dify_config.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY
        ):
            raise _CoordinationConflict("invalid_invited_attempt")
        policy = self._base._require_production_policy()
        if policy is not None and (
            native_contract != policy.native_token_contract
            or directory_contract != policy.directory_snapshot_contract
            or credential_strategy != policy.credential_strategy
        ):
            raise _CoordinationConflict("directory_contract_binding")
        config, _ = self._base._configuration(consumed, operation, "")
        validator = create_claims_validator(
            operation,
            namespace_id=consumed.context.namespace_id,
            revision_id=consumed.context.revision_id,
            diagnostic=False,
            redis_client=self._base._redis_client,
        )
        bundle = validator.verify_token_bundle(
            raw_tokens,
            expected_nonce=consumed.nonce,
            auth_started_at=consumed.auth_started_at,
            contract=native_contract,
            now=datetime.now(UTC),
        )
        _, context = self._base._configuration(consumed, operation, bundle.identity.subject)
        key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
        directory = CasdoorDirectoryGateway(
            operation,
            verified_subject=key.subject,
            credential_strategy=credential_strategy,
        )
        contract = _contract(directory_contract, config.organization)
        if (
            not contract.deployment_proof.matches(config)
            or directory.deployment_proof != contract.deployment_proof
            or native_contract.release_fingerprint != contract.deployment_proof.release_fingerprint
        ):
            raise _CoordinationConflict("directory_contract_binding")
        result, started, primary = None, False, None
        tokens = None
        finalization_outcome, token_outcome = "not_started", "not_started"
        cleanup = True
        for pass_number in range(2):
            leases = None
            try:
                profile = validator.verify_userinfo(
                    operation.userinfo(raw_tokens.payload["access_token"]),
                    identity=bundle.identity,
                )
                online = validator.verify_online_user(directory.get_verified_user(), identity=bundle.identity)
                operation._check()
                candidate = self._discover_recovery(context, key, consumed, profile)
                if candidate is not None:
                    if (
                        candidate.phase not in ("pending", "invitation_completed", "memberships_written", "finalized")
                        or self._session_gateway is None
                    ):
                        raise _CoordinationConflict("invitation_recovery_phase_pending")
                    preflight, plan = self._recovery_admission(consumed, bundle, online, profile, candidate)
                    leases = CasdoorLeases(
                        self._base._redis_client,
                        candidate.scope.lease_scope,
                        deadline=operation.deadline,
                    )
                    leases.acquire()
                    self._leases = leases
                    leases.ensure_owned()
                    if self._discover_recovery(context, key, consumed, profile) != candidate:
                        raise _InvitationScopeDrift()
                    fresh_profile = validator.verify_userinfo(
                        operation.userinfo(raw_tokens.payload["access_token"]),
                        identity=bundle.identity,
                    )
                    leases.ensure_owned()
                    roles = OnlineRoleSnapshotLoader(
                        operation,
                        identity=bundle.identity,
                        claims_validator=validator,
                        leases=leases,
                        credential_strategy=credential_strategy,
                        contract=directory_contract,
                    ).load()
                    fresh_online = VerifiedOnlineUser(roles.subject, roles.user_ref)
                    leases.ensure_owned()
                    _, current_context = self._base._configuration(consumed, operation, key.subject)
                    if current_context != context:
                        raise _CoordinationConflict("configuration_changed")
                    if (
                        fresh_profile != profile
                        or fresh_online != online
                        or self._recovery_admission(consumed, bundle, fresh_online, fresh_profile, candidate)
                        != (preflight, plan)
                        or self._discover_recovery(context, key, consumed, fresh_profile) != candidate
                    ):
                        raise _InvitationScopeDrift()
                    continuation = _InvitedRecoveryContinuation(
                        self,
                        consumed,
                        operation,
                        candidate,
                        bundle,
                        fresh_profile,
                        fresh_online,
                        roles,
                        config,
                        preflight,
                        plan,
                        leases,
                        correlation_id,
                        ip_address,
                    )
                    _ISSUED_RECOVERY_CONTINUATIONS.add(continuation)
                    # This is an observation of the original SQL operation, not
                    # a newly acknowledged producer/committed-pending wrapper.
                    result = candidate.snapshot
                    discovered = candidate
                    tokens = (
                        self._continue_invited_recovery(continuation)
                        if candidate.phase == "finalized"
                        else self._continue_invited_resume(continuation)
                    )
                    finalization_outcome, token_outcome = "committed", "issued"
                    break
                observed = self._store.observe_versioned_invitation(consumed.context.invite)
                if observed.status != "observed" or observed.observation is None:
                    raise _CoordinationConflict("invitation_authority_unavailable")
                observation = observed.observation
                # The actual original store registry is checked before any SQL
                # selector or projection can choose the invitation account.
                self._store._consumption_arguments(observation, str(uuid4()))
                discovered = self._discover(context, key, observation, profile)
                preflight, invitation, plan = self._admission(consumed, bundle, online, profile, discovered)
                shared = self._activation.prepare_invited_initialization(
                    InvitationLookup(None, None, consumed.context.invite),
                    setup=plan.setup,
                    authenticated_account_id=None,
                )
                prepared = self._invited_account._prepare_invited_login(
                    plan=plan,
                    preflight=preflight,
                    invitation=invitation,
                    shared=shared,
                    deadline=operation.deadline,
                )
                operation._check()
                leases = CasdoorLeases(
                    self._base._redis_client,
                    discovered.scope.lease_scope,
                    deadline=operation.deadline,
                )
                leases.acquire()
                self._leases = leases
                leases.ensure_owned()
                fresh = self._discover(context, key, observation, profile)
                leases.ensure_owned()
                if fresh != discovered:
                    raise _InvitationScopeDrift()
                fresh_profile = validator.verify_userinfo(
                    operation.userinfo(raw_tokens.payload["access_token"]),
                    identity=bundle.identity,
                )
                leases.ensure_owned()
                roles = OnlineRoleSnapshotLoader(
                    operation,
                    identity=bundle.identity,
                    claims_validator=validator,
                    leases=leases,
                    credential_strategy=credential_strategy,
                    contract=directory_contract,
                ).load()
                fresh_online = VerifiedOnlineUser(roles.subject, roles.user_ref)
                leases.ensure_owned()
                _, current_context = self._base._configuration(consumed, operation, key.subject)
                if current_context != context:
                    raise _CoordinationConflict("configuration_changed")
                current_admission = self._admission(consumed, bundle, fresh_online, fresh_profile, discovered)
                if (
                    fresh_profile != profile
                    or fresh_online != online
                    or current_admission != (preflight, invitation, plan)
                    or self._discover(context, key, observation, fresh_profile) != discovered
                ):
                    raise _InvitationScopeDrift()
                guard = _InvitedOperationGuard(
                    self,
                    consumed,
                    operation,
                    observation,
                    discovered,
                    prepared,
                    profile.email,
                )
                _ISSUED_OPERATION_GUARDS.add(guard)
                guard.ensure()
                started = True
                result = self._operation_owner._produce_invited_login_operation(
                    discovered.attempt,
                    token=consumed.context.invite,
                    prepared=prepared,
                    caller_guard=guard,
                )
                if self._session_gateway is not None:
                    continuation = _InvitedContinuation(
                        guard,
                        result,
                        bundle,
                        fresh_profile,
                        roles,
                        config,
                        correlation_id,
                        ip_address,
                    )
                    _ISSUED_CONTINUATIONS.add(continuation)
                    tokens = self._continue_invited_login(continuation)
                    finalization_outcome, token_outcome = "committed", "issued"
                break
            except _InvitationScopeDrift:
                pass
            except BaseException as error:
                finalization_outcome = getattr(error, "finalization_outcome", finalization_outcome)
                token_outcome = getattr(error, "token_outcome", token_outcome)
                primary = error
                raise
            finally:
                try:
                    if leases is not None:
                        cleanup = self._base._cleanup(leases, operation.deadline)
                except BaseException as error:
                    cleanup = False
                    if primary is None:
                        error.local_outcome = (
                            "committed" if result is not None else "unknown" if started else "not_started"
                        )
                        error.cleanup_released = False
                        error.finalization_outcome, error.token_outcome = (
                            finalization_outcome,
                            token_outcome,
                        )
                        raise
                finally:
                    self._leases = None
                    if primary is not None:
                        primary.local_outcome = (
                            "committed" if result is not None else "unknown" if started else "not_started"
                        )
                        primary.cleanup_released = cleanup
                        primary.finalization_outcome, primary.token_outcome = (
                            finalization_outcome,
                            token_outcome,
                        )
            if not cleanup or pass_number == 1:
                raise _CoordinationConflict("cleanup_pending" if not cleanup else "drift_limit")
        if result is None:
            raise _CoordinationConflict("drift_limit")
        return _InvitedLocalLoginOutcome(
            result,
            cleanup,
            finalization_outcome=finalization_outcome,
            token_outcome=token_outcome,
            tokens=tokens if cleanup else None,
            provenance=SessionProvenanceSeed(
                discovered.attempt.account_id,
                consumed.context.namespace_id,
                consumed.context.revision_id,
                float(bundle.identity.expires_at),
                self._base._optional_rp_token(config, consumed, bundle, raw_tokens),
            )
            if cleanup and type(tokens) is AuthTokenPair
            else None,
        )
