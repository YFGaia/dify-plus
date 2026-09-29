"""Create Dify Plus workflow-run ownership metadata table.

Revision ID: 020_workflow_run_account
Revises: 019_webapp_auth_switch
Create Date: 2026-09-29 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "020_workflow_run_account"
down_revision = "019_webapp_auth_switch"
branch_labels = None
depends_on = None


def upgrade():
    if not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table("workflow_run_account_extend"):
        return

    op.create_table(
        "workflow_run_account_extend",
        sa.Column("workflow_run_id", StringUUID(), nullable=False),
        sa.Column("tenant_id", StringUUID(), nullable=False),
        sa.Column("app_id", StringUUID(), nullable=False),
        sa.Column("from_account_id", StringUUID(), nullable=True),
        sa.PrimaryKeyConstraint("workflow_run_id", name="workflow_run_account_extend_pkey"),
    )
    op.create_index(
        "workflow_run_account_extend_scope_idx",
        "workflow_run_account_extend",
        ["tenant_id", "app_id", "from_account_id", "workflow_run_id"],
    )


def downgrade():
    if not op.get_context().as_sql and not sa.inspect(op.get_bind()).has_table("workflow_run_account_extend"):
        return
    op.drop_index("workflow_run_account_extend_scope_idx", table_name="workflow_run_account_extend")
    op.drop_table("workflow_run_account_extend")
