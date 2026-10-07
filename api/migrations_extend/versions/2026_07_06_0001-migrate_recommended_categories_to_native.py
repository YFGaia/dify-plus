"""migrate_recommended_categories_to_native

Revision ID: 017_migrate_recommended_cats
Revises: 016_drop_gva_admin_tables
Create Date: 2026-07-06

一次性数据迁移（DD3，openspec change `p3-merge-upstream-1-15-0`）：
把 fork 自建分类表 `recommended_category_extend` / `recommended_apps_category_join_extend`
中的分类关系写入上游原生 `recommended_apps.categories` JSON 列
（该列由上游迁移 `a4f2d8c9b731` 建立，1.14.2 已进库；本迁移必须在
`flask db upgrade` 之后执行——即 DD7 三段命令链的既定顺序）。

- 幂等：写入前与目标列现有值合并去重，重复执行不产生重复分类项。
- 全新部署两张 fork 表不存在时安全跳过。
- 本迁移只做数据搬运，不 drop 表；drop 由下一版本
  `018_drop_recommended_category_tables` 单独执行（可暂缓）。
- downgrade 为 no-op：数据迁移不可逆（合并写入后无法区分哪些分类来自
  fork 表、哪些原生已有），回滚依赖升级前的数据库快照。
"""

import json

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "017_migrate_recommended_cats"
down_revision = "016_drop_gva_admin_tables"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # 全新部署从未建过 fork 分类表，无数据可迁
    if "recommended_category_extend" not in tables or "recommended_apps_category_join_extend" not in tables:
        return

    category_column = conn.dialect.identifier_preparer.quote_identifier("table")
    rows = conn.execute(
        sa.text(
            f"""
            SELECT j.recommended_id AS recommended_id, c.{category_column} AS category
            FROM recommended_apps_category_join_extend j
            JOIN recommended_category_extend c ON c.id = j.category_id
            """
        )
    ).fetchall()

    categories_by_recommended: dict[str, set[str]] = {}
    for row in rows:
        categories_by_recommended.setdefault(str(row.recommended_id), set()).add(row.category)

    for recommended_id, fork_categories in categories_by_recommended.items():
        existing_raw = conn.execute(
            sa.text("SELECT categories FROM recommended_apps WHERE id = :id"),
            {"id": recommended_id},
        ).scalar()
        # categories 是 sa.JSON 列：驱动可能返回已解码的 list、原始 JSON 字符串或 NULL
        if isinstance(existing_raw, str):
            existing = json.loads(existing_raw)
        elif isinstance(existing_raw, list):
            existing = existing_raw
        else:
            existing = []

        # 保留原生列已有顺序，fork 分类按字典序补在后面；已存在的不重复追加（幂等关键）
        merged = list(existing)
        for category in sorted(fork_categories):
            if category not in merged:
                merged.append(category)

        if merged != existing:
            conn.execute(
                sa.text("UPDATE recommended_apps SET categories = CAST(:categories AS json) WHERE id = :id"),
                {"categories": json.dumps(merged, ensure_ascii=False), "id": recommended_id},
            )


def downgrade():
    # 数据迁移不可逆（见模块 docstring），回滚依赖升级前快照；fork 表在 018 执行前数据仍完整保留
    pass
