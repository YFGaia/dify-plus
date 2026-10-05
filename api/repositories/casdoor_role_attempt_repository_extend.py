"""Reserve one durable local slot, never remote dispatch or termination authority.

The caller owns the complete transaction and must roll it back on any failure.
This owner does not flush, commit, dispatch, retry, renew or finalize anything.
Its separate conservative fence marks an exact reservation IN_FLIGHT/UNCONFIRMED
without sending. Reentry observes that quarantine only; it grants no send or
retry opportunity, including after an unknown caller commit acknowledgement.
A reserved NOT_STARTED row says only this owner has issued no dispatch transition;
it cannot establish absence or termination of an independently executed request.

The bounded internal dependency on I14-A's private guards and serialization is
intentional: that owner remains the single policy owner. The canonical scope
assembly is reused directly; the related-intent query mirrors its persistence contract and is tested
against actual enqueue output; enqueue itself must never restage a reserved row.
"""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from core.casdoor.mapping import DesiredWorkspaceTarget
from core.casdoor.ownership import MAX_SNAPSHOT_BYTES
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from sqlalchemy.orm import Session

from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_role_intent_repository_extend import (
    CasdoorRoleIntentConflict,
    CasdoorRoleIntentRepository,
)


@dataclass(frozen=True, repr=False)
class RoleAttemptReservation:
    """Local persistence only, uncommitted until the caller commits its UoW."""

    intent_id: UUID
    reservation_id: UUID
    membership_id: UUID
    join_id: UUID
    desired_payload_digest: str
    scope_digest: str


@dataclass(frozen=True, repr=False)
class RoleAttemptFenceObservation:
    """Uncommitted SQL quarantine observation, never dispatch or recovery authority."""

    intent_id: UUID
    reservation_id: UUID
    membership_id: UUID
    join_id: UUID
    desired_payload_digest: str
    scope_digest: str


class CasdoorRoleAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._guard = CasdoorRoleIntentRepository(session)

    def reserve(
        self,
        version: GenerationPlanVersion,
        target: DesiredWorkspaceTarget,
        *,
        intent_id: UUID,
        reservation_id: UUID,
    ) -> RoleAttemptReservation:
        """Allocate once or read the exact unchanged reservation; never retry.

        Sorted complete caller leases and trusted plan reconstruction remain
        caller obligations. The count records a reserved slot, not HTTP calls.
        Future execution requires separate authority and a committed conservative
        IN_FLIGHT/UNCONFIRMED transition before I/O, with trusted I16 termination
        before recovery can permit another attempt. None is supplied here.
        """
        self._guard._require_clean_root()
        self._guard._validate(version, target)
        if not isinstance(intent_id, UUID) or not isinstance(reservation_id, UUID):
            raise CasdoorRoleIntentConflict()
        with self._session.no_autoflush:
            scope, scope_digest, values, join, history, row = self._read_attempt_scope(version, target, intent_id)
            reserved = dict(values, attempt_count=1, attempt_id=str(reservation_id))
            actual = {name: row[name] for name in values}
            if actual != reserved:
                if actual != values:
                    raise CasdoorRoleIntentConflict()
                result = self._session.execute(
                    sa.update(Intent)
                    .where(
                        Intent.id == str(intent_id),
                        *(getattr(Intent, name) == value for name, value in values.items()),
                    )
                    .values(attempt_count=1, attempt_id=str(reservation_id))
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorRoleIntentConflict()
            return RoleAttemptReservation(
                intent_id,
                reservation_id,
                UUID(history.id),
                UUID(join.id),
                scope["desired_payload_digest"],
                scope_digest,
            )

    def _read_attempt_scope(self, version, target, intent_id):
        """Reuse the accepted guard chain and sole-related-intent query for both operations."""
        self._guard._guard_owner(version, target)
        self._guard._saved_mapping(version, target)
        join, history = self._guard._scope(version, target)
        c = version.plan.context
        scope, scope_digest, key, values = self._guard._role_intent_scope(version, target, join, history)
        refs = tuple(
            self._session.execute(
                sa.select(Intent.id, Intent.idempotency_key)
                .where(
                    sa.or_(
                        Intent.idempotency_key == key,
                        sa.and_(
                            Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                            sa.or_(
                                sa.and_(
                                    Intent.account_id == str(c.account_id),
                                    sa.or_(
                                        Intent.workspace_id == str(target.workspace_id),
                                        Intent.workspace_id.is_(None),
                                    ),
                                ),
                                Intent.membership_id == history.id,
                            ),
                        ),
                    )
                )
                .order_by(Intent.namespace_id, Intent.scope_digest, Intent.id)
                .limit(2)
                .with_for_update()
            )
        )
        if len(refs) != 1 or refs[0].id != str(intent_id) or refs[0].idempotency_key != key:
            raise CasdoorRoleIntentConflict()
        self._guard._bounded_text(Intent, str(intent_id), (Intent.desired_json,), MAX_SNAPSHOT_BYTES)
        stored = self._session.execute(
            sa.select(*Intent.__table__.columns).where(Intent.id == str(intent_id)).with_for_update()
        ).one()
        return scope, scope_digest, values, join, history, dict(stored._mapping)

    def mark_dispatch_unconfirmed(
        self,
        version: GenerationPlanVersion,
        target: DesiredWorkspaceTarget,
        *,
        intent_id: UUID,
        reservation_id: UUID,
    ) -> RoleAttemptFenceObservation:
        """Quarantine one exact reserved attempt without sending or owning the commit.

        The caller owns complete sorted leases, the whole root rollback and the
        eventual commit. This observation cannot enable a sender. A caller with
        an unknown commit acknowledgement must not rearm or dispatch: an exact
        already-armed row is only read back, never reset or counted again.
        """
        self._guard._require_clean_root()
        self._guard._validate(version, target)
        if type(intent_id) is not UUID or type(reservation_id) is not UUID:
            raise CasdoorRoleIntentConflict()
        with self._session.no_autoflush:
            scope, scope_digest, values, join, history, before = self._read_attempt_scope(version, target, intent_id)
            reserved = dict(values, attempt_count=1, attempt_id=str(reservation_id))
            armed = dict(
                reserved,
                operation_state=CasdoorOperationState.IN_FLIGHT,
                termination_state=CasdoorTerminationState.UNCONFIRMED,
            )
            actual = {name: before[name] for name in values}
            if actual != armed:
                if actual != reserved:
                    raise CasdoorRoleIntentConflict()
                # Match all columns, including timestamps, so the two-state CAS
                # cannot silently overwrite a concurrent scope or effect change.
                result = self._session.execute(
                    sa.update(Intent)
                    .where(*(getattr(Intent, name) == value for name, value in before.items()))
                    .values(
                        operation_state=CasdoorOperationState.IN_FLIGHT,
                        termination_state=CasdoorTerminationState.UNCONFIRMED,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise CasdoorRoleIntentConflict()
            # Reconstruct the complete current owner chain, not just the changed
            # row: even a SQL trigger changing another parent must abort the UoW.
            after_scope, after_digest, after_values, after_join, after_history, after = self._read_attempt_scope(
                version, target, intent_id
            )
            if (
                after_scope != scope
                or after_digest != scope_digest
                or after_values != values
                or after_join != join
                or after_history != history
                or {name: after[name] for name in values} != armed
                or any(
                    after[name] != value
                    for name, value in before.items()
                    if name not in ("operation_state", "termination_state", "updated_at")
                )
                or (actual == armed and after != before)
            ):
                raise CasdoorRoleIntentConflict()
            return RoleAttemptFenceObservation(
                intent_id,
                reservation_id,
                UUID(history.id),
                UUID(join.id),
                scope["desired_payload_digest"],
                scope_digest,
            )
