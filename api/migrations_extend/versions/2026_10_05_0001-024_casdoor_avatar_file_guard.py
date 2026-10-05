"""Add permanent per-file UUID avatar coordination parents.

Revision ID: 024_casdoor_avatar_file_guard
Revises: 023_invitation_authority
Create Date: 2026-10-05 00:01:00.000000

No seed, backfill, foreign key, provider setting or cleanup authority is created.
Downgrade drops only this table; it is not safe concurrent parent garbage collection.
"""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "024_casdoor_avatar_file_guard"
down_revision = "023_invitation_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "casdoor_avatar_file_guard_extend",
        sa.Column("file_id", StringUUID(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("intent_id", StringUUID(), nullable=True),
        sa.Column("attempt_id", StringUUID(), nullable=True),
        sa.PrimaryKeyConstraint("file_id", name="casdoor_avatar_file_guard_pkey"),
        sa.CheckConstraint(
            "version >= 1 AND version <= 9007199254740991",
            name=op.f("casdoor_avatar_file_guard_extend_version_range_check"),
        ),
        sa.CheckConstraint(
            "stage IN ('unbound', 'reserved', 'cleanup_pending', 'cleanup_complete')",
            name=op.f("casdoor_avatar_file_guard_extend_stage_check"),
        ),
        sa.CheckConstraint(
            "(stage = 'unbound' AND intent_id IS NULL AND attempt_id IS NULL) OR "
            "(stage <> 'unbound' AND intent_id IS NOT NULL AND attempt_id IS NOT NULL)",
            name=op.f("casdoor_avatar_file_guard_extend_binding_shape_check"),
        ),
    )


def downgrade():
    op.drop_table("casdoor_avatar_file_guard_extend")
