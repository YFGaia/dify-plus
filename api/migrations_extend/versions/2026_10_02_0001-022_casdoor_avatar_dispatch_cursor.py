"""Add navigation-only initial avatar cursor and portable scan index.

Revision ID: 022_casdoor_avatar_cursor
Revises: 021_casdoor_integration
Create Date: 2026-10-02 00:01:00.000000

No seed rows or authority grants. Downgrade removes only these new objects;
navigation does not prove publication, completion or termination of any work.
"""

import sqlalchemy as sa
from alembic import op
from models.types import StringUUID

revision = "022_casdoor_avatar_cursor"
down_revision = "021_casdoor_integration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "casdoor_avatar_dispatch_cursor_extend",
        sa.Column("slot", sa.SmallInteger(), nullable=False, autoincrement=False),
        sa.Column("last_id", StringUUID(), nullable=True),
        sa.Column("sweep_upper_id", StringUUID(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("slot", name="casdoor_avatar_dispatch_cursor_extend_pkey"),
        sa.CheckConstraint("slot = 1", name=op.f("casdoor_avatar_dispatch_cursor_extend_singleton_slot_check")),
        sa.CheckConstraint(
            "version >= 0 AND version <= 9223372036854775807",
            name=op.f("casdoor_avatar_dispatch_cursor_extend_version_range_check"),
        ),
        sa.CheckConstraint(
            "(sweep_upper_id IS NULL AND last_id IS NULL) OR "
            "(sweep_upper_id IS NOT NULL AND (last_id IS NULL OR last_id <= sweep_upper_id))",
            name=op.f("casdoor_avatar_dispatch_cursor_extend_sweep_shape_check"),
        ),
    )
    op.create_index(
        "casdoor_avatar_initial_scan_idx",
        "casdoor_sync_intent_extend",
        ["kind", "operation_state", "termination_state", "attempt_count", "id"],
    )


def downgrade():
    op.drop_index("casdoor_avatar_initial_scan_idx", table_name="casdoor_sync_intent_extend")
    op.drop_table("casdoor_avatar_dispatch_cursor_extend")
