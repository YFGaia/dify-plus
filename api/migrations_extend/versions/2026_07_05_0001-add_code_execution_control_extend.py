"""add_code_execution_control_extend

Revision ID: 015_code_execution_control
Revises: 014_message_context_extend
Create Date: 2026-07-05

code 节点 sandbox-full 授权邮箱名单（openspec change p5-admin-decommission）。
DB 为 source of truth，redis 键 control_mail 是投影缓存，
写入方为 services.system_manage_extend.CodeExecutionControlService。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

from models import types

# revision identifiers, used by Alembic.
revision = "015_code_execution_control"
down_revision = "014_message_context_extend"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # MySQL requires an expression default; keep PostgreSQL UUIDv4 generation unchanged.
    uuid_default = sa.text("(UUID())") if conn.dialect.name == "mysql" else sa.text("uuid_generate_v4()")
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if "code_execution_control_extend" not in tables:
        op.create_table(
            "code_execution_control_extend",
            sa.Column("id", types.StringUUID(), server_default=uuid_default, nullable=False),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("created_by", types.StringUUID(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP(0)"), nullable=False),
            sa.PrimaryKeyConstraint("id", name="code_execution_control_extend_pkey"),
            sa.UniqueConstraint("email", name="code_execution_control_extend_email_key"),
        )


def downgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if "code_execution_control_extend" in tables:
        op.drop_table("code_execution_control_extend")
