"""add_webapp_auth_enabled_to_app_extend

Revision ID: 019_webapp_auth_switch
Revises: 018_drop_recommended_cats
Create Date: 2026-07-21

WebApp 访问认证开关（per-app）：`app_extend.webapp_auth_enabled`。
NULL/True = 访问已发布 WebApp 需登录 Console（fork 默认行为），False = 允许匿名访问。
读写方为 services.webapp_auth_service_extend.WebAppAuthExtendService，
redis 键 webapp_auth_enabled_extend:{app_id} 是投影缓存（写后删除、读侧回填）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "019_webapp_auth_switch"
down_revision = "018_drop_recommended_cats"
branch_labels = None
depends_on = None


def _get_columns(inspector: Inspector) -> set[str]:
    return {column["name"] for column in inspector.get_columns("app_extend")}


def upgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    if "app_extend" not in inspector.get_table_names():
        return

    if "webapp_auth_enabled" not in _get_columns(inspector):
        with op.batch_alter_table("app_extend", schema=None) as batch_op:
            batch_op.add_column(sa.Column("webapp_auth_enabled", sa.Boolean(), nullable=True))


def downgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    if "app_extend" not in inspector.get_table_names():
        return

    if "webapp_auth_enabled" in _get_columns(inspector):
        with op.batch_alter_table("app_extend", schema=None) as batch_op:
            batch_op.drop_column("webapp_auth_enabled")
