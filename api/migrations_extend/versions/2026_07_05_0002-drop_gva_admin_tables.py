"""drop_gva_admin_tables

Revision ID: 016_drop_gva_admin_tables
Revises: 015_code_execution_control
Create Date: 2026-07-05

一次性清理 gin-vue-admin（admin/ 管理后台）遗留的框架表。
背景见 openspec change `p5-admin-decommission`（admin 废弃）：

- admin 服务已从 docker compose 移除，GVA 框架表（`sys_*`、`casbin_rule`、
  `exa_*`、`jwt_blacklists`）与全局代码授权表（`sys_user_global_code(s)`）
  不再有任何写入方或读取方。
- `sys_user_global_code(s)` 的存量数据必须先经 `flask extend_db-migrate-code-execution-control`
  （或等价的存量迁移，任务 2.6/4.2）迁入 `code_execution_control_extend` 后再执行本迁移。
- 执行前 MUST 用 pg_dump 导出全部待 DROP 表并归档备份（人工确认，见 tasks 10.1）。
- gaia 业务表（`batch_workflows_extend`、`batch_workflow_tasks_extend` 冷备保留，
  见 docs/dify-plus/批量工作流数据表冷备归档说明.md；`app_version_*`、
  `model_provider_config_extend`、`model_proxy_log_extend`、`account_ding_talk_extend`、
  `app_request_test*` 等按 P1/D3 决策单独处置）不混入本迁移。

表名单来自 admin/server/initialize/gorm.go 的 AutoMigrate 列表 + 模型 many2many
连接表（GORM 配置 singular: false，默认复数化；有 TableName() 重载的按重载值）。
`sys_user_global_code`/`sys_user_global_codes` 单复数两个名字都列入，
兼容不同历史部署（设计文档用单数，GORM 默认生成复数）。

downgrade 不可恢复（GVA 表结构属外部 Go 服务，本仓库无建表定义），
回滚依赖执行前的 pg_dump 备份。
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "016_drop_gva_admin_tables"
down_revision = "015_code_execution_control"
branch_labels = None
depends_on = None

# 逐表列名单（不用通配），按 spec admin-decommission-cleanup「GVA 数据库表清理」要求
GVA_TABLES_TO_DROP: list[str] = [
    # GVA 框架 many2many 连接表（先删，避免部分数据库存在外键）
    "sys_authority_menus",
    "sys_user_authority",
    "sys_data_authority_id",
    "sys_authority_btns",
    # GVA 框架主表
    "sys_apis",
    "sys_ignore_apis",
    "sys_users",
    "sys_base_menus",
    "sys_base_menu_parameters",
    "sys_base_menu_btns",
    "sys_authorities",
    "sys_dictionaries",
    "sys_dictionary_details",
    "sys_operation_records",
    "sys_auto_code_histories",
    "sys_auto_code_packages",
    "sys_export_templates",
    "sys_export_template_condition",
    "sys_export_template_join",
    "sys_params",
    "jwt_blacklists",
    # casbin 权限规则表（gorm-adapter）
    "casbin_rule",
    # GVA 示例模块表
    "exa_files",
    "exa_customers",
    "exa_file_chunks",
    "exa_file_upload_and_downloads",
    # 全局代码授权表（存量已迁入 code_execution_control_extend）
    "sys_user_global_code",
    "sys_user_global_codes",
]


def upgrade():
    conn = op.get_bind()
    quote = conn.dialect.identifier_preparer.quote_identifier
    cascade = "" if conn.dialect.name == "mysql" else " CASCADE"
    for table in GVA_TABLES_TO_DROP:
        # IF EXISTS：全新部署从未运行过 GVA，表不存在属正常
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {quote(table)}{cascade}"))


def downgrade():
    raise NotImplementedError(
        "GVA admin tables are dropped irreversibly; restore from the pg_dump "
        "backup taken before running this migration (see tasks 10.1 of "
        "openspec change p5-admin-decommission)."
    )
