"""Add original invitation lifecycle tombstones and immutable issuance facts.

Revision ID: 023_invitation_authority
Revises: 022_casdoor_avatar_cursor
Create Date: 2026-10-03 00:01:00.000000

No foreign keys, seed rows, legacy tokens, or backfill. Downgrade removes only
these two new tables; it is not an invitation cancellation mechanism.
"""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "023_invitation_authority"
down_revision = "022_casdoor_avatar_cursor"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invitation_authority_lifecycle_extend",
        sa.Column("lifecycle_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("workspace_id", StringUUID(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("lifecycle_id", name="invitation_authority_lifecycle_pkey"),
        sa.UniqueConstraint("account_id", "workspace_id", name="invitation_authority_lifecycle_identity_key"),
        sa.CheckConstraint(
            "epoch >= 1 AND epoch <= 9007199254740991",
            name=op.f("invitation_authority_lifecycle_extend_epoch_range_check"),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'withdrawn')", name=op.f("invitation_authority_lifecycle_extend_state_check")
        ),
    )
    op.create_table(
        "invitation_authority_issuance_extend",
        sa.Column("issuance_id", StringUUID(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("workspace_id", StringUUID(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(255), nullable=False),
        sa.Column("requires_setup", sa.Boolean(), nullable=False),
        sa.Column("lifecycle_id", StringUUID(), nullable=False),
        sa.Column("lifecycle_epoch", sa.BigInteger(), nullable=False),
        sa.Column("join_id_at_issue", StringUUID(), nullable=True),
        sa.Column("actor_id", StringUUID(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("consumption_receipt_json", sa.Text(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("issuance_id", name="invitation_authority_issuance_pkey"),
        sa.UniqueConstraint("token_digest", name="invitation_authority_issuance_token_digest_key"),
        sa.UniqueConstraint("payload_digest", name="invitation_authority_issuance_payload_digest_key"),
        sa.CheckConstraint(
            "lifecycle_epoch >= 1 AND lifecycle_epoch <= 9007199254740991",
            name=op.f("invitation_authority_issuance_extend_epoch_range_check"),
        ),
        sa.CheckConstraint(
            "state IN ('issued', 'consumed')", name=op.f("invitation_authority_issuance_extend_state_check")
        ),
        sa.CheckConstraint(
            "length(token_digest) = 64 AND token_digest = lower(token_digest)",
            name=op.f("invitation_authority_issuance_extend_token_digest_check"),
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest = lower(payload_digest)",
            name=op.f("invitation_authority_issuance_extend_payload_digest_check"),
        ),
        sa.CheckConstraint(
            "(state = 'issued' AND consumption_receipt_json IS NULL AND consumed_at IS NULL) OR "
            "(state = 'consumed' AND consumption_receipt_json IS NOT NULL AND consumed_at IS NOT NULL)",
            name=op.f("invitation_authority_issuance_extend_consumption_shape_check"),
        ),
    )


def downgrade():
    op.drop_table("invitation_authority_issuance_extend")
    op.drop_table("invitation_authority_lifecycle_extend")
