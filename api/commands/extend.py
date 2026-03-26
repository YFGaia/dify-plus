"""
extend: 管理二开扩展表的数据库迁移命令组
将 extend_db 命令从旧的 commands.py 迁移至此，保持与 commands/ 目录结构一致
"""

import click


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
