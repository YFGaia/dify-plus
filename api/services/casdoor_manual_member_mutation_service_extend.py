"""Private metadata bridge for authorized COMMUNITY LOCAL member writers.

The existing writer owns authorization, the complete affected member set, actual
permission changes and transaction completion. This bridge cannot terminate an
intent or authorize a member action. The original repository supplies the
locked, single-use preparation and metadata CAS in that same caller root.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from configs import dify_config
from core.casdoor.manual_ownership import ManualMemberScope, ManualMutationKind
from core.casdoor.ownership import MembershipBackend
from enums import DeploymentEdition
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipConflict,
    CasdoorManualOwnershipRepository,
)
from services.errors.account import NoPermissionError


def _local_manual_mode() -> bool:
    return (
        dify_config.DEPLOYMENT_EDITION == DeploymentEdition.COMMUNITY
        and not dify_config.RBAC_ENABLED
    )


def require_clean_local_manual_session(session: Session) -> None:
    """Reject dirty state before the original admission queries can autoflush it.

    The original reads may start the root through autobegin; prepare subsequently
    requires that actual active root. No transaction is opened or completed here.
    """
    if _local_manual_mode() and (
        not session.is_active
        or session.get_nested_transaction() is not None
        or session.new
        or session.dirty
        or session.deleted
    ):
        raise NoPermissionError("Casdoor manual membership authorization pending.")


def mark_local_manual_member_mutation(
    session: Session,
    *,
    workspace_id: str,
    account_ids: tuple[str, ...],
    kind: ManualMutationKind,
) -> None:
    """Consume actual preparation before the authorized writer dirties any Join.

    IDs come from the writer's actual resolved database members. They are scope,
    not capabilities. Retained completed required intents still deny this action.
    """
    if not _local_manual_mode():
        return
    try:
        scopes = tuple(
            ManualMemberScope(UUID(account_id), UUID(workspace_id))
            for account_id in account_ids
        )
        repository = CasdoorManualOwnershipRepository(session)
        preparation = repository.prepare(
            scopes, kind=kind, backend=MembershipBackend.LOCAL
        )
        repository.mark_local_override(preparation)
    except (CasdoorManualOwnershipConflict, ValueError, RuntimeError):
        raise NoPermissionError(
            "Casdoor manual membership authorization pending."
        ) from None
