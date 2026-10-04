"""Durable original invitation facts; no account or membership authority is granted.

Identifiers intentionally have no foreign keys so withdrawn history survives
deletion of the account, workspace, or join. Payload text preserves the exact
signed bytes used by P1's payload digest; receipts contain no email or token.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from libs.datetime_utils import naive_utc_now
from models.base import Base
from models.types import StringUUID


class InvitationAuthorityLifecycleExtend(Base):
    __tablename__ = "invitation_authority_lifecycle_extend"
    __table_args__ = (
        sa.PrimaryKeyConstraint("lifecycle_id", name="invitation_authority_lifecycle_pkey"),
        sa.UniqueConstraint("account_id", "workspace_id", name="invitation_authority_lifecycle_identity_key"),
        sa.CheckConstraint("epoch >= 1 AND epoch <= 9007199254740991", name="epoch_range"),
        sa.CheckConstraint("state IN ('active', 'withdrawn')", name="state"),
    )

    lifecycle_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    workspace_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=naive_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime, default=naive_utc_now, nullable=False)


class InvitationAuthorityIssuanceExtend(Base):
    __tablename__ = "invitation_authority_issuance_extend"
    __table_args__ = (
        sa.PrimaryKeyConstraint("issuance_id", name="invitation_authority_issuance_pkey"),
        sa.UniqueConstraint("token_digest", name="invitation_authority_issuance_token_digest_key"),
        sa.UniqueConstraint("payload_digest", name="invitation_authority_issuance_payload_digest_key"),
        sa.CheckConstraint("lifecycle_epoch >= 1 AND lifecycle_epoch <= 9007199254740991", name="epoch_range"),
        sa.CheckConstraint("state IN ('issued', 'consumed')", name="state"),
        sa.CheckConstraint("length(token_digest) = 64 AND token_digest = lower(token_digest)", name="token_digest"),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest = lower(payload_digest)", name="payload_digest"
        ),
        sa.CheckConstraint(
            "(state = 'issued' AND consumption_receipt_json IS NULL AND consumed_at IS NULL) OR "
            "(state = 'consumed' AND consumption_receipt_json IS NOT NULL AND consumed_at IS NOT NULL)",
            name="consumption_shape",
        ),
    )

    issuance_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    token_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    workspace_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    requires_setup: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    lifecycle_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    lifecycle_epoch: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    join_id_at_issue: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    payload_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    consumption_receipt_json: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=naive_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime, default=naive_utc_now, nullable=False)
