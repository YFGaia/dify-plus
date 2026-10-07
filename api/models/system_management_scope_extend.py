"""Durable instance-wide management workspace identity.

Workspace names and login defaults are presentation/provisioning data. This
singleton records the workspace created during installation and never elects a
replacement. RESTRICT preserves that identity when workspace deletion is tried.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import StringUUID


class SystemManagementScopeExtend(Base):
    __tablename__ = "system_management_scope_extend"
    __table_args__ = (sa.CheckConstraint("id = 'initialization'", name="system_management_scope_singleton"),)

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default="initialization")
    tenant_id: Mapped[str] = mapped_column(StringUUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)
