"""drop_recommended_category_tables

Revision ID: 018_drop_recommended_cats
Revises: 017_migrate_recommended_cats
Create Date: 2026-07-06

删除 fork 自建分类表 `recommended_category_extend` / `recommended_apps_category_join_extend`
（DD3 第三步，openspec change `p3-merge-upstream-1-15-0`）。

!!! 执行时机（重要）!!!
仅在探索页分类回归（tasks 9.4）通过后执行本版本；在此之前部署可停在
017 版本：`flask extend_db upgrade 017_migrate_recommended_cats`。
分类数据已由 017 迁入 `recommended_apps.categories`，drop 前 fork 表数据
天然保留，可随时回退代码切换而不丢数据。

- 两表的模型类（RecommendedCategoryExtend / RecommendedAppsCategoryJoinExtend）
  已随代码切换从 models/model_extend.py 移除，migrations_extend 不会按模型自动建表。
- downgrade 仅重建空表结构（schema 同 002_recommended_apps_category），
  数据恢复依赖执行前的数据库快照 / pg_dump 备份。
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "018_drop_recommended_cats"
down_revision = "017_migrate_recommended_cats"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # IF EXISTS：全新部署从未建过 fork 分类表
    conn.execute(sa.text('DROP TABLE IF EXISTS "recommended_apps_category_join_extend"'))
    conn.execute(sa.text('DROP TABLE IF EXISTS "recommended_category_extend"'))


def downgrade():
    # 仅重建空表结构（迁移自包含，用裸 SQL 而非应用模型）；数据恢复依赖快照备份
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS "recommended_category_extend" (
                id uuid DEFAULT uuid_generate_v4() NOT NULL,
                "table" varchar(255) NOT NULL,
                tag_id uuid,
                CONSTRAINT category_extend_id_pkey PRIMARY KEY (id)
            )
            """
        )
    )
    conn.execute(sa.text('CREATE INDEX IF NOT EXISTS idx_extend_table ON "recommended_category_extend" ("table")'))
    conn.execute(
        sa.text('CREATE INDEX IF NOT EXISTS idx_extend_tag_bind_tag_id ON "recommended_category_extend" (tag_id)')
    )
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS "recommended_apps_category_join_extend" (
                id uuid DEFAULT uuid_generate_v4() NOT NULL,
                recommended_id uuid NOT NULL,
                category_id uuid NOT NULL,
                CONSTRAINT recommended_apps_category_id_pkey PRIMARY KEY (id)
            )
            """
        )
    )
    conn.execute(
        sa.text(
            'CREATE INDEX IF NOT EXISTS idx_recommended_id ON "recommended_apps_category_join_extend" (recommended_id)'
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_recommended_category_id "
            'ON "recommended_apps_category_join_extend" (category_id)'
        )
    )
