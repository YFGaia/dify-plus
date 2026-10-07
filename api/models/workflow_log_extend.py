"""Session-bound fork display identities for workflow application logs."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.account import Account
from models.model import EndUser
from models.model_extend import EndUserAccountJoinsExtend


@dataclass(frozen=True)
class WorkflowLogUserExtend:
    id: str
    type: str
    is_anonymous: bool
    session_id: str


def workflow_log_users_extend(
    *, session: Session, tenant_id: str, app_id: str, end_user_ids: Sequence[str]
) -> dict[str, WorkflowLogUserExtend]:
    """Batch the old fork display mapping; never return an ORM object beyond this session.

    Explicit app/end-user mappings take precedence over historical external UUIDs.
    These are presentation fields only: the persisted creator role remains unchanged.
    """
    account_ids: dict[str, str] = {}
    if end_user_ids:
        end_users = session.execute(
            select(EndUser.id, EndUser.external_user_id).where(
                EndUser.id.in_(end_user_ids), EndUser.tenant_id == tenant_id, EndUser.app_id == app_id
            )
        ).all()
        scoped_ids = [user.id for user in end_users]
        if scoped_ids:
            joins = session.execute(
                select(EndUserAccountJoinsExtend.end_user_id, EndUserAccountJoinsExtend.account_id)
                .where(
                    EndUserAccountJoinsExtend.end_user_id.in_(scoped_ids), EndUserAccountJoinsExtend.app_id == app_id
                )
                .order_by(EndUserAccountJoinsExtend.created_at, EndUserAccountJoinsExtend.id)
            )
            explicit_ids: dict[str, str] = {}
            for join in joins:
                explicit_ids.setdefault(join.end_user_id, join.account_id)
            for end_user in end_users:
                if end_user.id in explicit_ids:
                    account_ids[end_user.id] = explicit_ids[end_user.id]
                elif end_user.external_user_id:
                    try:
                        account_ids[end_user.id] = str(UUID(end_user.external_user_id))
                    except ValueError:
                        pass

    if not account_ids:
        return {}
    accounts = session.scalars(select(Account).where(Account.id.in_(set(account_ids.values())))).all()
    displays = {
        account.id: WorkflowLogUserExtend(
            id=account.id, type=account.status, is_anonymous=True, session_id=account.name
        )
        for account in accounts
    }
    return {
        creator_id: displays[account_id] for creator_id, account_id in account_ids.items() if account_id in displays
    }
