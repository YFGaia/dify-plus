"""Fork quota initialization within the caller's account creation transaction."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from configs import dify_config
from models.account_money_extend import AccountMoneyExtend


def ensure_account_quota_extend(account_id: str, *, session: Session) -> None:
    """Create a missing quota without committing or resetting an existing balance."""
    existing = session.scalar(select(AccountMoneyExtend).where(AccountMoneyExtend.account_id == account_id))
    if existing is None:
        session.add(
            AccountMoneyExtend(
                id=str(uuid4()), account_id=account_id, total_quota=dify_config.ACCOUNT_TOTAL_QUOTA, used_quota=0
            )
        )
