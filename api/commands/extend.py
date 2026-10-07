"""
extend: 管理二开扩展表的数据库迁移命令组
将 extend_db 命令从旧的 commands.py 迁移至此，保持与 commands/ 目录结构一致
"""

import logging

import click

logger = logging.getLogger(__name__)


@click.group("extend_db", help="管理二开扩展表的数据库迁移")
def extend_db():
    """管理二开扩展表的数据库迁移"""
    pass


@extend_db.command("upgrade", help="将数据库升级到最新版本")
@click.option("--revision", default="head", help="目标版本，默认为最新版本(head)")
def extend_db_upgrade(revision):
    """将数据库升级到指定版本（默认为最新版本）"""
    _run_alembic_command_extend("upgrade", revision)


@extend_db.command("downgrade", help="回滚数据库到指定版本")
@click.option("--revision", required=True, help="目标版本")
def extend_db_downgrade(revision):
    """回滚数据库到指定版本"""
    _run_alembic_command_extend("downgrade", revision)


@extend_db.command("current", help="显示当前数据库版本")
def extend_db_current():
    """显示当前数据库版本"""
    _run_alembic_command_extend("current")


@extend_db.command("history", help="显示迁移历史")
def extend_db_history():
    """显示迁移历史"""
    _run_alembic_command_extend("history")


@extend_db.command("heads", help="显示最新的迁移版本")
def extend_db_heads():
    """显示最新的迁移版本"""
    _run_alembic_command_extend("heads")


@extend_db.command(
    "migrate-code-execution-control",
    help="将 GVA sys_user_global_code 授权名单（经 sys_users.email 映射）一次性迁入 "
    "code_execution_control_extend 并重建 redis control_mail 缓存。幂等可重跑。",
)
def migrate_code_execution_control():
    """存量数据一次性迁移（openspec p5-admin-decommission 任务 2.6 / D-4）。

    行为契约：
    - GVA 源表（sys_user_global_code 单/复数任一 + sys_users）不存在时 info 提示并跳过，不报错；
    - 已存在于新表的邮箱跳过（幂等）；
    - 迁移后调用 CodeExecutionControlService.rebuild_control_mail_cache() 同步 redis，
      并输出「新表总行数 vs 源表去重行数」核对结果；
    - 必须在 DROP sys_user_global_code（016_drop_gva_admin_tables）之前执行。
    """
    import sqlalchemy as sa

    from extensions.ext_database import db
    from models.system_extend import CodeExecutionControlExtend
    from services.system_manage_extend import CodeExecutionControlService

    def _table_exists(table_name: str) -> bool:
        return bool(
            db.session.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = :table_name"
                ),
                {"table_name": table_name},
            ).first()
        )

    # GVA（GORM 默认复数化）历史部署可能建成 sys_user_global_codes，两个名字都探测
    source_table = next(
        (name for name in ("sys_user_global_code", "sys_user_global_codes") if _table_exists(name)),
        None,
    )
    if source_table is None or not _table_exists("sys_users"):
        click.echo(
            click.style(
                "GVA source tables (sys_user_global_code[s] / sys_users) not found, nothing to migrate.",
                fg="yellow",
            )
        )
        logger.info("migrate-code-execution-control skipped: GVA source tables not found")
        return

    # 源表去重邮箱名单（strip+lower 规范化后再去重，与 service 写路径语义一致）
    rows = db.session.execute(
        sa.text(
            # source_table 取自上方固定白名单（非用户输入），f-string 拼接无注入风险
            f"SELECT DISTINCT email FROM sys_users "
            f"WHERE id IN (SELECT user_id FROM {source_table}) AND email IS NOT NULL"
        )
    ).all()
    source_emails = {normalized for row in rows if (normalized := (row.email or "").strip().lower())}

    migrated = 0
    skipped = 0
    for email in sorted(source_emails):
        exists = db.session.query(CodeExecutionControlExtend).filter(CodeExecutionControlExtend.email == email).first()
        if exists:
            skipped += 1
            continue
        # created_by 留空：源数据无法映射到 Dify account 操作者
        db.session.add(CodeExecutionControlExtend(email=email, created_by=None))
        migrated += 1
    db.session.commit()

    cache_synced = CodeExecutionControlService.rebuild_control_mail_cache()

    total_in_table = db.session.query(CodeExecutionControlExtend).count()
    logger.info(
        "migrate-code-execution-control done: source=%s distinct=%d migrated=%d skipped=%d cache_synced=%s",
        source_table,
        len(source_emails),
        migrated,
        skipped,
        cache_synced,
    )
    click.echo(
        click.style(
            f"Migrated {migrated} email(s) from {source_table} ({skipped} already present, "
            f"source distinct count: {len(source_emails)}).",
            fg="green",
        )
    )
    check_ok = migrated + skipped == len(source_emails) and total_in_table >= len(source_emails)
    click.echo(
        click.style(
            f"Verification: table now holds {total_in_table} row(s), "
            f"covers all {len(source_emails)} source email(s): {'OK' if check_ok else 'MISMATCH'}. "
            f"Redis control_mail cache synced: {cache_synced}.",
            fg="green" if check_ok and cache_synced else "yellow",
        )
    )


def _run_alembic_command_extend(command, *args):
    """运行 alembic 命令"""
    import os

    from alembic import command as alembic_command
    from alembic.config import Config
    from flask import current_app

    # 获取 api 目录的绝对路径（commands/ 的上层就是 api/）
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    migrations_extend_dir = os.path.join(api_dir, "migrations_extend")

    # 创建 alembic 配置
    alembic_cfg = Config(os.path.join(migrations_extend_dir, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", migrations_extend_dir)

    # 获取相应的 alembic 命令函数
    cmd_func = getattr(alembic_command, command)

    # 在 Flask 应用上下文中执行 alembic 命令
    with current_app.app_context():
        if args:
            cmd_func(alembic_cfg, *args)
        else:
            cmd_func(alembic_cfg)
