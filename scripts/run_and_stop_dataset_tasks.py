#!/usr/bin/env python3
"""
运行排队中的任务和停止执行中的任务

用法:
    python scripts/run_and_stop_dataset_tasks.py [--run-waiting] [--stop-running]
    
选项:
    --run-waiting: 运行所有排队中的任务（waiting状态）
    --stop-running: 停止所有执行中的任务（parsing, cleaning, splitting, indexing状态）
    
如果不指定选项，将同时执行两个操作。

示例:
    python scripts/run_and_stop_dataset_tasks.py --run-waiting
    python scripts/run_and_stop_dataset_tasks.py --stop-running
    python scripts/run_and_stop_dataset_tasks.py
"""

import os
import sys
import argparse
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


def run_waiting_tasks():
    """运行所有排队中的任务（waiting状态）"""
    
    # 创建 Flask 应用并初始化数据库连接
    app = create_app()
    
    with app.app_context():
        # 查询所有处于waiting状态的文档（排除已暂停的）
        waiting_documents = db.session.query(Document).filter(
            Document.indexing_status == 'waiting',
            Document.is_paused != True  # 只处理未暂停的文档（False 或 None）
        ).all()
        
        if not waiting_documents:
            print("✓ 没有找到需要运行的排队任务。")
            return
        
        print(f"找到 {len(waiting_documents)} 个排队中的文档：")
        for doc in waiting_documents[:10]:  # 只显示前10个
            print(f"  - 文档 ID: {doc.id}, 名称: {doc.name[:50]}, 数据集: {doc.dataset_id}")
        if len(waiting_documents) > 10:
            print(f"  ... (还有 {len(waiting_documents) - 10} 个文档)")
        
        # 确认提示
        print(f"\n⚠️  将触发 {len(waiting_documents)} 个排队文档的索引任务")
        confirmation = input("是否继续？(yes/no): ")
        
        if confirmation.lower() not in ['yes', 'y']:
            print("操作已取消。")
            return
        
        print("\n开始触发索引任务...")
        
        submitted_count = 0
        failed_count = 0
        
        # 按数据集分组，减少任务数量
        dataset_docs = {}
        for doc in waiting_documents:
            if doc.dataset_id not in dataset_docs:
                dataset_docs[doc.dataset_id] = []
            dataset_docs[doc.dataset_id].append(doc)
        
        for dataset_id, docs in dataset_docs.items():
            try:
                # 获取第一个文档的tenant_id（同一数据集下的文档应该有相同的tenant_id）
                tenant_id = docs[0].tenant_id
                document_ids = [doc.id for doc in docs]
                
                # 尝试使用新的任务函数
                task_submitted = False
                try:
                    from tasks.document_indexing_task import normal_document_indexing_task, priority_document_indexing_task
                    # 使用普通优先级任务
                    task = normal_document_indexing_task.delay(
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                        document_ids=document_ids
                    )
                    print(f"  ✓ 数据集 {dataset_id}: 已提交 {len(document_ids)} 个文档的索引任务 (任务ID: {task.id})")
                    submitted_count += len(document_ids)
                    task_submitted = True
                except (ImportError, AttributeError) as e:
                    # 尝试使用旧版API
                    try:
                        from tasks.document_indexing_task import document_indexing_task
                        task = document_indexing_task.delay(dataset_id, document_ids)
                        print(f"  ✓ 数据集 {dataset_id}: 已提交 {len(document_ids)} 个文档的索引任务（使用旧版 API）(任务ID: {task.id})")
                        submitted_count += len(document_ids)
                        task_submitted = True
                    except Exception as e2:
                        print(f"  ✗ 数据集 {dataset_id}: 无法提交索引任务: {e2}")
                        failed_count += len(document_ids)
                
                if not task_submitted:
                    failed_count += len(document_ids)
                    
            except Exception as e:
                print(f"  ✗ 错误：处理数据集 {dataset_id} 时出错: {e}")
                failed_count += len(docs)
                continue
        
        print(f"\n✓ 成功提交 {submitted_count} 个文档的索引任务")
        if failed_count > 0:
            print(f"⚠️  有 {failed_count} 个文档提交失败。")


def stop_running_tasks():
    """停止所有执行中的任务（parsing, cleaning, splitting, indexing状态）"""
    
    # 创建 Flask 应用并初始化数据库连接
    app = create_app()
    
    with app.app_context():
        # 定义需要停止的状态
        # parsing, cleaning, splitting, indexing: 执行中
        running_statuses = ['parsing', 'cleaning', 'splitting', 'indexing']
        
        # 查询所有处于执行中的文档（排除已经暂停的）
        running_documents = db.session.query(Document).filter(
            Document.indexing_status.in_(running_statuses),
            Document.is_paused != True  # 只处理未暂停的文档（False 或 None）
        ).all()
        
        if not running_documents:
            print("✓ 没有找到需要停止的执行中任务。")
            return
        
        # 统计各状态的文档数量
        status_count = {}
        for doc in running_documents:
            status = doc.indexing_status
            status_count[status] = status_count.get(status, 0) + 1
        
        print(f"找到 {len(running_documents)} 个执行中的文档：")
        for status, count in status_count.items():
            status_name = {
                'parsing': '解析中',
                'cleaning': '清理中',
                'splitting': '分割中',
                'indexing': '索引中'
            }.get(status, status)
            print(f"  - {status_name} ({status}): {count} 个")
        
        # 确认提示
        print("\n⚠️  警告：此操作将停止所有执行中的文档处理，文档将被设置为暂停状态！")
        confirmation = input("是否继续？(yes/no): ")
        
        if confirmation.lower() not in ['yes', 'y']:
            print("操作已取消。")
            return
        
        print("\n开始停止文档处理...")
        
        updated_count = 0
        failed_count = 0
        for doc in running_documents:
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


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='运行排队中的任务和停止执行中的任务',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --run-waiting      # 只运行排队中的任务
  %(prog)s --stop-running    # 只停止执行中的任务
  %(prog)s                    # 同时执行两个操作
        """
    )
    parser.add_argument(
        '--run-waiting',
        action='store_true',
        help='运行所有排队中的任务（waiting状态）'
    )
    parser.add_argument(
        '--stop-running',
        action='store_true',
        help='停止所有执行中的任务（parsing, cleaning, splitting, indexing状态）'
    )
    
    args = parser.parse_args()
    
    # 如果没有指定任何选项，默认执行两个操作
    if not args.run_waiting and not args.stop_running:
        args.run_waiting = True
        args.stop_running = True
    
    print("=" * 60)
    print("数据集任务管理脚本")
    print("=" * 60)
    
    if args.run_waiting:
        print("\n[1/2] 运行排队中的任务")
        print("-" * 60)
        run_waiting_tasks()
    
    if args.stop_running:
        print("\n[2/2] 停止执行中的任务")
        print("-" * 60)
        stop_running_tasks()
    
    print("\n" + "=" * 60)
    print("操作完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
