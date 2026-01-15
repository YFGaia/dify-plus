#!/usr/bin/env python3
"""
停止所有排队中和执行中的知识库文档，将它们设置为暂停状态

用法:
    python scripts/fail_pending_documents.py
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
# 支持两种运行环境：
# 1. 本地开发：scripts/ 在项目根目录下，需要添加 api/ 子目录
# 2. 容器环境：脚本直接在 api/ 目录下，直接使用当前目录
script_file = Path(__file__).resolve()
script_dir = script_file.parent

# 查找 api 目录（包含 app_factory.py 和 libs 目录的目录）
api_path = None
current_dir = script_dir

# 向上查找 api 目录，最多查找 3 层
for _ in range(3):
    app_factory = current_dir / "app_factory.py"
    datetime_utils = current_dir / "libs" / "datetime_utils.py"
    if app_factory.exists() and datetime_utils.exists():
        api_path = current_dir
        break
    current_dir = current_dir.parent

# 如果没找到，尝试 scripts 目录的父目录下的 api 子目录
if api_path is None:
    potential_api_path = script_dir.parent / "api"
    app_factory = potential_api_path / "app_factory.py"
    datetime_utils = potential_api_path / "libs" / "datetime_utils.py"
    if app_factory.exists() and datetime_utils.exists():
        api_path = potential_api_path

if api_path is None:
    raise ImportError(
        f"无法找到 API 目录（包含 app_factory.py 和 libs/datetime_utils.py）。\n"
        f"脚本位置: {script_file}\n"
        f"脚本目录: {script_dir}\n"
        f"已检查的目录: {script_dir}, {script_dir.parent}, {script_dir.parent / 'api'}"
    )

# 确保 api 目录在 Python 路径的最前面，并切换到该目录
api_path_str = str(api_path.resolve())
if api_path_str not in sys.path:
    sys.path.insert(0, api_path_str)

# 切换到 api 目录（这样相对导入会更可靠）
os.chdir(api_path_str)

from app_factory import create_app
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from libs.datetime_utils import naive_utc_now
from models.dataset import Document


def stop_pending_documents():
    """停止所有排队中和执行中的知识库文档，设置为暂停状态"""
    
    # 创建 Flask 应用并初始化数据库连接
    app = create_app()
    
    with app.app_context():
        # 定义需要停止的状态
        # waiting: 排队中
        # parsing, cleaning, splitting, indexing: 执行中
        pending_statuses = ['waiting', 'parsing', 'cleaning', 'splitting', 'indexing']
        
        # 查询所有处于排队中或执行中的文档（排除已经暂停的）
        from sqlalchemy import false
        pending_documents = db.session.query(Document).filter(
            Document.indexing_status.in_(pending_statuses),
            Document.is_paused != True  # 只处理未暂停的文档（False 或 None）
        ).all()
        
        if not pending_documents:
            print("✓ 没有找到需要停止的文档。")
            return
        
        # 统计各状态的文档数量
        status_count = {}
        for doc in pending_documents:
            status = doc.indexing_status
            status_count[status] = status_count.get(status, 0) + 1
        
        print(f"找到 {len(pending_documents)} 个排队中或执行中的文档：")
        for status, count in status_count.items():
            status_name = {
                'waiting': '排队中',
                'parsing': '解析中',
                'cleaning': '清理中',
                'splitting': '分割中',
                'indexing': '索引中'
            }.get(status, status)
            print(f"  - {status_name} ({status}): {count} 个")
        
        # 确认提示
        print("\n⚠️  警告：此操作将停止所有排队中和执行中的文档处理，文档将被设置为暂停状态！")
        confirmation = input("是否继续？(yes/no): ")
        
        if confirmation.lower() not in ['yes', 'y']:
            print("操作已取消。")
            return
        
        print("\n开始停止文档处理...")
        
        updated_count = 0
        failed_count = 0
        for doc in pending_documents:
            try:
                old_status = doc.indexing_status
                # 设置文档为暂停状态
                doc.is_paused = True
                doc.paused_at = naive_utc_now()
                # paused_by 可以为 None（系统操作）
                doc.paused_by = None
                # 设置停止时间
                doc.stopped_at = naive_utc_now()
                
                # 在 Redis 中设置暂停标志（如果 Redis 可用）
                try:
                    indexing_cache_key = f"document_{doc.id}_is_paused"
                    redis_client.setnx(indexing_cache_key, "True")
                except Exception:
                    # Redis 不可用时继续执行
                    pass
                
                updated_count += 1
                if updated_count <= 10:  # 只显示前10个详细信息
                    print(f"  - 文档 ID: {doc.id}, 名称: {doc.name[:50]}, 状态: {old_status} -> paused")
                elif updated_count == 11:
                    print("  ... (更多文档正在更新)")
            except Exception as e:
                failed_count += 1
                print(f"  - ✗ 错误：停止文档 {doc.id} 时出错: {e}")
                continue
        
        # 提交更改
        try:
            db.session.commit()
            print(f"\n✓ 成功停止 {updated_count} 个文档的处理（已设置为暂停状态）。")
            if failed_count > 0:
                print(f"⚠️  有 {failed_count} 个文档停止失败。")
        except Exception as e:
            db.session.rollback()
            print(f"\n✗ 错误：提交更改时出错: {e}")
            sys.exit(1)


if __name__ == "__main__":
    stop_pending_documents()

