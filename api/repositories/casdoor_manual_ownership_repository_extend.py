"""Caller-owned, local no-intent manual ownership metadata foundation.

This does not hook or authorize existing member writers. Caller authorization
precedes a clean short root UoW: prepare -> mark -> actual local member mutation
-> commit. Any rejection or subsequent writer failure must escape and roll back
the WHOLE UoW. Do not catch and commit or roll back only a metadata savepoint.
No commit/rollback, permission/member/account writes, network or cancellation.
Parent locks serialize only participants obeying the shared lock protocol; I12-B
writer wiring, I13 remote completeness, I16 termination and real PG/MySQL races
remain separate. Archived namespaces and unlinked historical identities survive.
"""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.manual_ownership import (
    ManualHistoryObservation,
    ManualMemberScope,
    ManualMutationKind,
    ManualOwnershipDecision,
    ManualScopeInspection,
    decide_manual_ownership,
    validate_manual_scopes,
)
from core.casdoor.ownership import MembershipBackend, MembershipObservation
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
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
    CasdoorMembershipOwnership,
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
from sqlalchemy.orm import Session, SessionTransaction

from repositories.casdoor_generation_repository_extend import MAX_GENERATION

MAX_PARENT_ROWS = 2000
MAX_INTENT_ROWS = 20000


class CasdoorManualOwnershipConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("authorization_pending")


@dataclass(frozen=True, repr=False)
class _LockedState:
    integrations: tuple
    namespaces: tuple
    accounts: tuple
    identities: tuple
    workspaces: tuple
    joins: tuple
    histories: tuple
    intents: tuple
    revisions: tuple


@dataclass(frozen=True, repr=False)
class ManualMutationPreparation:
    """One-use issuer registry token bound to the exact root transaction.

    Public fields/types are observations, never permission or proof. The private
    registry, full fresh lock recheck and metadata CAS enforce consumption.
    """

    scopes: tuple[ManualMemberScope, ...]
    kind: ManualMutationKind
    backend: MembershipBackend
    inspections: tuple[ManualScopeInspection, ...]
    transaction: SessionTransaction
    _state: _LockedState


@dataclass(frozen=True, repr=False)
class ManualOverrideReceipt:
    membership_id: UUID
    namespace_id: UUID
    account_id: UUID
    workspace_id: UUID
    retained_join_id: UUID | None
    ownership_epoch: int
    tombstone: bool


class CasdoorManualOwnershipRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._preparations: dict[int, ManualMutationPreparation] = {}

    def prepare(
        self,
        scopes: tuple[ManualMemberScope, ...],
        *,
        kind: ManualMutationKind,
        backend: MembershipBackend,
    ) -> ManualMutationPreparation:
        """Inspect ALL namespace histories under bounded sorted parent locks.

        ROLE_CHANGE/REMOVE usually have one scope; bounded batches require the
        caller's complete affected set. Transfer requires exactly two different
        accounts in one workspace. Scope tuple completeness is not authorization.
        Account status is never changed or treated as manual-action admission.
        """
        self._require_clean_root()
        try:
            validate_manual_scopes(scopes, kind)
        except ValueError:
            raise CasdoorManualOwnershipConflict() from None
        if not isinstance(backend, MembershipBackend):
            raise CasdoorManualOwnershipConflict()
        scopes = tuple(sorted(scopes, key=lambda scope: (str(scope.workspace_id), str(scope.account_id))))
        with self._session.no_autoflush:
            state = self._read_locked(scopes)
            inspections = self._inspect(scopes, kind, backend, state)
            if kind is ManualMutationKind.OWNER_TRANSFER:
                roles = [join.role for join in state.joins]
                if len(roles) != 2 or roles.count(TenantAccountRole.OWNER) != 1:
                    raise CasdoorManualOwnershipConflict()
        transaction = self._session.get_transaction()
        assert transaction is not None
        token = ManualMutationPreparation(scopes, kind, backend, inspections, transaction, state)
        self._preparations[id(token)] = token
        return token

    def mark_local_override(self, token: ManualMutationPreparation) -> tuple[ManualOverrideReceipt, ...]:
        """CAS only actual MANAGED rows; tombstone retains the historical join ID.

        Baseline/last-applied/desired TEXT and finalization are neither loaded nor
        rewritten. One-use even on failure. No fake termination proof parameter.
        """
        self._require_clean_root()
        if not isinstance(token, ManualMutationPreparation) or self._preparations.pop(id(token), None) is not token:
            raise CasdoorManualOwnershipConflict()
        if self._session.get_transaction() is not token.transaction:
            raise CasdoorManualOwnershipConflict()
        with self._session.no_autoflush:
            state = self._read_locked(token.scopes)
            if (
                state != token._state
                or self._inspect(token.scopes, token.kind, token.backend, state) != token.inspections
            ):
                raise CasdoorManualOwnershipConflict()
            if any(
                view.decision
                not in (
                    ManualOwnershipDecision.MANAGED_READY,
                    ManualOwnershipDecision.PRESERVE_UNMANAGED,
                    ManualOwnershipDecision.ALREADY_LOCAL,
                )
                for view in token.inspections
            ):
                raise CasdoorManualOwnershipConflict()
            managed = [row for row in state.histories if row.ownership is CasdoorMembershipOwnership.MANAGED]
            if any(row.ownership_epoch >= MAX_GENERATION for row in managed):
                raise CasdoorManualOwnershipConflict()
            receipts = []
            for row in managed:
                tombstone = token.kind is ManualMutationKind.MEMBER_REMOVE
                result = self._session.execute(
                    sa.update(History)
                    .where(
                        History.id == row.id,
                        History.namespace_id == row.namespace_id,
                        History.identity_id == row.identity_id,
                        History.account_id == row.account_id,
                        History.workspace_id == row.workspace_id,
                        History.revision_id == row.revision_id,
                        History.join_id == row.join_id,
                        History.ownership == CasdoorMembershipOwnership.MANAGED,
                        History.ownership_epoch == row.ownership_epoch,
                        History.desired_generation == row.desired_generation,
                        History.tombstone == row.tombstone,
                    )
                    .values(
                        ownership=CasdoorMembershipOwnership.LOCAL_OVERRIDE,
                        ownership_epoch=row.ownership_epoch + 1,
                        tombstone=tombstone,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorManualOwnershipConflict()
                receipts.append(
                    ManualOverrideReceipt(
                        UUID(row.id),
                        UUID(row.namespace_id),
                        UUID(row.account_id),
                        UUID(row.workspace_id),
                        UUID(row.join_id) if row.join_id else None,
                        row.ownership_epoch + 1,
                        tombstone,
                    )
                )
            return tuple(receipts)

    def _require_clean_root(self) -> None:
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not self._session.is_active
            or self._session.get_nested_transaction() is not None
        ):
            raise RuntimeError("Casdoor manual ownership requires a clean active caller-owned root transaction")
        if self._session.new or self._session.dirty or self._session.deleted:
            raise CasdoorManualOwnershipConflict()

    def _rows(self, statement, limit=MAX_PARENT_ROWS, *, lock=True) -> tuple:
        if lock:
            statement = statement.with_for_update()
        rows = tuple(self._session.execute(statement.limit(limit + 1)))
        if len(rows) > limit:
            raise CasdoorManualOwnershipConflict()
        return rows

    @staticmethod
    def _scope_filter(model, scopes):
        return sa.or_(
            *(sa.and_(model.account_id == str(s.account_id), model.workspace_id == str(s.workspace_id)) for s in scopes)
        )

    def _read_locked(self, scopes: tuple[ManualMemberScope, ...]) -> _LockedState:
        # Fresh COLUMN rows avoid identity-map staleness and loading history TEXT.
        integrations = self._rows(
            sa.select(Integration.id, Integration.slot, Integration.etag).order_by(Integration.id), 1
        )
        namespaces = self._rows(
            sa.select(
                Namespace.id,
                Namespace.integration_id,
                Namespace.fence_epoch,
                Namespace.lifecycle,
                Namespace.expected_issuer,
                Namespace.organization,
                Namespace.application,
                Namespace.client_id,
            ).order_by(Namespace.id)
        )
        if any(row.slot != 1 or not self._epoch(row.etag) for row in integrations):
            raise CasdoorManualOwnershipConflict()
        integration_id = integrations[0].id if integrations else None
        ns = {row.id: row for row in namespaces}
        if any(row.integration_id != integration_id or not self._epoch(row.fence_epoch) for row in namespaces):
            raise CasdoorManualOwnershipConflict()
        # Discovery is a read under integration/namespace parents, not a history
        # lock ahead of account/identity. Detect set changes at final history read.
        discovery = self._rows(
            sa.select(History.id, History.identity_id)
            .where(self._scope_filter(History, scopes))
            .order_by(History.namespace_id, History.account_id, History.workspace_id, History.id),
            lock=False,
        )
        account_ids = sorted({str(scope.account_id) for scope in scopes})
        workspace_ids = sorted({str(scope.workspace_id) for scope in scopes})
        accounts = self._rows(
            sa.select(Account.id, Account.status).where(Account.id.in_(account_ids)).order_by(Account.id)
        )
        if {row.id for row in accounts} != set(account_ids):
            raise CasdoorManualOwnershipConflict()
        historical_identity_ids = {row.identity_id for row in discovery}
        identities = self._rows(
            sa.select(Identity.id, Identity.namespace_id, Identity.account_id, Identity.issuer, Identity.organization)
            .where(sa.or_(Identity.account_id.in_(account_ids), Identity.id.in_(historical_identity_ids)))
            .order_by(Identity.namespace_id, Identity.id)
        )
        identity_map = {row.id: row for row in identities}
        if any(
            row.namespace_id not in ns
            or row.account_id not in account_ids
            or row.issuer != ns[row.namespace_id].expected_issuer
            or row.organization != ns[row.namespace_id].organization
            for row in identities
        ):
            raise CasdoorManualOwnershipConflict()
        workspaces = self._rows(
            sa.select(Tenant.id, Tenant.status).where(Tenant.id.in_(workspace_ids)).order_by(Tenant.id)
        )
        if {row.id for row in workspaces} != set(workspace_ids):
            raise CasdoorManualOwnershipConflict()
        joins = self._rows(
            sa.select(
                TenantAccountJoin.id, TenantAccountJoin.account_id, TenantAccountJoin.tenant_id, TenantAccountJoin.role
            )
            .where(
                sa.or_(
                    *(
                        sa.and_(
                            TenantAccountJoin.account_id == str(s.account_id),
                            TenantAccountJoin.tenant_id == str(s.workspace_id),
                        )
                        for s in scopes
                    )
                )
            )
            .order_by(TenantAccountJoin.tenant_id, TenantAccountJoin.account_id, TenantAccountJoin.id)
        )
        if len({(row.account_id, row.tenant_id) for row in joins}) != len(joins) or any(
            not isinstance(row.role, TenantAccountRole) for row in joins
        ):
            raise CasdoorManualOwnershipConflict()
        histories = self._rows(
            sa.select(
                History.id,
                History.namespace_id,
                History.identity_id,
                History.account_id,
                History.workspace_id,
                History.join_id,
                History.revision_id,
                History.ownership,
                History.ownership_epoch,
                History.desired_generation,
                History.source,
                History.finalization,
                History.tombstone,
            )
            .where(self._scope_filter(History, scopes))
            .order_by(History.namespace_id, History.account_id, History.workspace_id, History.id)
        )
        if {(row.id, row.identity_id) for row in histories} != {(row.id, row.identity_id) for row in discovery}:
            raise CasdoorManualOwnershipConflict()
        intents = self._rows(
            sa.select(
                Intent.id, Intent.namespace_id, Intent.identity_id, Intent.account_id, Intent.workspace_id, Intent.kind
            )
            .where(
                Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                sa.or_(
                    self._scope_filter(Intent, scopes),
                    sa.and_(Intent.account_id.in_(account_ids), Intent.workspace_id.is_(None)),
                    Intent.membership_id.in_([row.id for row in histories]),
                ),
            )
            .order_by(Intent.namespace_id, Intent.scope_digest, Intent.id),
            MAX_INTENT_ROWS,
        )
        revision_ids = {row.revision_id for row in histories}
        revisions = self._rows(
            sa.select(
                Revision.id,
                Revision.integration_id,
                Revision.namespace_id,
                Revision.expected_issuer,
                Revision.organization,
                Revision.application,
                Revision.client_id,
            )
            .where(Revision.id.in_(revision_ids))
            .order_by(Revision.id),
            lock=False,
        )
        revision_map = {row.id: row for row in revisions}
        for row in histories:
            namespace = ns.get(row.namespace_id)
            revision = revision_map.get(row.revision_id)
            identity = identity_map.get(row.identity_id)
            if (
                namespace is None
                or revision is None
                or revision.integration_id != integration_id
                or revision.namespace_id != row.namespace_id
                or tuple(revision)[3:] != tuple(namespace)[4:]
                or not self._epoch(row.ownership_epoch)
                or not self._epoch(row.desired_generation)
                or (
                    identity is not None
                    and (identity.namespace_id, identity.account_id) != (row.namespace_id, row.account_id)
                )
            ):
                raise CasdoorManualOwnershipConflict()
        if any(row.namespace_id not in ns or row.account_id not in account_ids for row in intents):
            raise CasdoorManualOwnershipConflict()
        try:
            for row in (*histories, *intents):
                for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id"):
                    value = getattr(row, field)
                    if value is not None:
                        UUID(value)
            for row in histories:
                UUID(row.revision_id)
                if row.join_id is not None:
                    UUID(row.join_id)
        except (ValueError, TypeError, AttributeError):
            raise CasdoorManualOwnershipConflict() from None
        return _LockedState(
            integrations, namespaces, accounts, identities, workspaces, joins, histories, intents, revisions
        )

    @staticmethod
    def _epoch(value) -> bool:
        return type(value) is int and 0 <= value <= MAX_GENERATION

    @staticmethod
    def _inspect(scopes, kind, backend, state) -> tuple[ManualScopeInspection, ...]:
        inspections = []
        for scope in scopes:
            join = next(
                (
                    row
                    for row in state.joins
                    if (row.account_id, row.tenant_id) == (str(scope.account_id), str(scope.workspace_id))
                ),
                None,
            )
            history = tuple(
                ManualHistoryObservation(
                    UUID(row.id),
                    row.ownership,
                    row.ownership_epoch,
                    UUID(row.join_id) if row.join_id else None,
                    row.tombstone,
                )
                for row in state.histories
                if (row.account_id, row.workspace_id) == (str(scope.account_id), str(scope.workspace_id))
            )
            # A cross-scope malformed historical intent blocks the entire batch,
            # never gains permission through an incorrectly scoped association.
            intent_ids = tuple(UUID(row.id) for row in state.intents)
            observation = MembershipObservation(
                scope.workspace_id,
                scope.account_id,
                UUID(join.id) if join else None,
                join.role if join else None,
                backend,
            )
            inspections.append(
                ManualScopeInspection(
                    scope,
                    decide_manual_ownership(observation, history, required_intent_ids=intent_ids, kind=kind),
                    history,
                )
            )
        return tuple(inspections)
