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
    inspector = None if op.get_context().as_sql else sa.inspect(op.get_bind())
    table_exists = inspector is not None and inspector.has_table("workflow_run_account_extend")
    if not table_exists:
        op.create_table(
            "workflow_run_account_extend",
            sa.Column("workflow_run_id", StringUUID(), nullable=False),
            sa.Column("tenant_id", StringUUID(), nullable=False),
            sa.Column("app_id", StringUUID(), nullable=False),
            sa.Column("from_account_id", StringUUID(), nullable=True),
            sa.PrimaryKeyConstraint("workflow_run_id", name="workflow_run_account_extend_pkey"),
        )
    # A previous attempt may have created the table without reaching the index.
    indexes = inspector.get_indexes("workflow_run_account_extend") if inspector is not None and table_exists else []
    if not any(index["name"] == "workflow_run_account_extend_scope_idx" for index in indexes):
        op.create_index(
            "workflow_run_account_extend_scope_idx",
            "workflow_run_account_extend",
            ["tenant_id", "app_id", "from_account_id", "workflow_run_id"],
        )


def downgrade():
    inspector = None if op.get_context().as_sql else sa.inspect(op.get_bind())
    if inspector is not None and not inspector.has_table("workflow_run_account_extend"):
        return
    if inspector is None or any(
        index["name"] == "workflow_run_account_extend_scope_idx"
        for index in inspector.get_indexes("workflow_run_account_extend")
    ):
        op.drop_index("workflow_run_account_extend_scope_idx", table_name="workflow_run_account_extend")
    op.drop_table("workflow_run_account_extend")
