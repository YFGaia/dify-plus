"""Permanent file-UUID coordination parents; these rows grant no storage authority.

No foreign key or expiry removes a parent while an avatar writer can await it.
Original reservation/audit owners retain tenant scope and all proof material.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base
from models.types import StringUUID


class CasdoorAvatarFileGuardExtend(Base):
    __tablename__ = "casdoor_avatar_file_guard_extend"
    __table_args__ = (
        sa.PrimaryKeyConstraint("file_id", name="casdoor_avatar_file_guard_pkey"),
        sa.CheckConstraint("version >= 1 AND version <= 9007199254740991", name="version_range"),
        sa.CheckConstraint("stage IN ('unbound', 'reserved', 'cleanup_pending', 'cleanup_complete')", name="stage"),
        sa.CheckConstraint(
            "(stage = 'unbound' AND intent_id IS NULL AND attempt_id IS NULL) OR "
            "(stage <> 'unbound' AND intent_id IS NOT NULL AND attempt_id IS NOT NULL)",
            name="binding_shape",
        ),
    )

    file_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    version: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    stage: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    intent_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
