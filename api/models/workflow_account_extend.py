"""Dify Plus ownership metadata for workflow runs."""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import StringUUID


class WorkflowRunAccountExtend(Base):
    """Immutable Console actor, including an explicit NULL for anonymous runs."""

    __tablename__ = "workflow_run_account_extend"
    __table_args__ = (
        sa.PrimaryKeyConstraint("workflow_run_id", name="workflow_run_account_extend_pkey"),
        sa.Index("workflow_run_account_extend_scope_idx", "tenant_id", "app_id", "from_account_id", "workflow_run_id"),
    )

    workflow_run_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    tenant_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    app_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    from_account_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
