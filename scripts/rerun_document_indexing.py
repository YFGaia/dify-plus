#!/usr/bin/env python3
"""
重新运行文档索引任务

用法:
    python scripts/rerun_document_indexing.py [文档名称或ID]
    
示例:
    python scripts/rerun_document_indexing.py "change UK.txt"
    python scripts/rerun_document_indexing.py 1cbd33e6-eccd-4548-b82d-e527ad23ac15
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "api"))

from app_factory import create_app
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from libs.datetime_utils import naive_utc_now
from models.dataset import Document, DocumentSegment


def rerun_document_indexing(document_name_or_id: str = None, use_priority: bool = True):
    """重新运行文档索引任务"""
    
    # 创建 Flask 应用并初始化数据库连接
    app = create_app()
    
    with app.app_context():
        # 查找文档
        if document_name_or_id:
            # 尝试按名称查找
            documents = db.session.query(Document).filter(
                Document.name.like(f"%{document_name_or_id}%")
            ).all()
            
            # 如果按名称没找到，尝试按ID查找
            if not documents:
                documents = db.session.query(Document).filter(
                    Document.id == document_name_or_id
                ).all()
        else:
            # 如果没有指定，查找所有状态为 indexing、error 或 paused 的文档
            documents = db.session.query(Document).filter(
                Document.indexing_status.in_(['indexing', 'error', 'paused'])
            ).all()
            
            if not documents:
                print("没有找到需要重新索引的文档。")
                print("请指定文档名称或ID，例如：")
                print('  python scripts/rerun_document_indexing.py "change UK.txt"')
                return
        
        if not documents:
            print(f"未找到文档: {document_name_or_id}")
            return
        
        print(f"找到 {len(documents)} 个文档：\n")
        for doc in documents:
            print(f"  - {doc.id}: {doc.name}")
            print(f"    数据集: {doc.dataset_id}")
            print(f"    当前状态: {doc.indexing_status}")
            print(f"    是否暂停: {doc.is_paused}")
            print()
        
        # 确认
        if len(documents) > 1:
            print(f"⚠️  将重新索引 {len(documents)} 个文档")
        else:
            print(f"⚠️  将重新索引文档: {documents[0].name}")
        confirmation = input("是否继续？(yes/no): ")
        
        if confirmation.lower() not in ['yes', 'y']:
            print("操作已取消。")
            return
        
        print("\n开始重置文档状态并重新索引...\n")
        
        for doc in documents:
            try:
                print(f"处理文档: {doc.name} ({doc.id})")
                
                # 重置文档状态
                doc.indexing_status = 'waiting'
                doc.is_paused = False
                doc.paused_by = None
                doc.paused_at = None
                doc.error = None
                doc.stopped_at = None
                doc.processing_started_at = None
                doc.completed_at = None
                
                # 清除 Redis 中的暂停标志
                try:
                    indexing_cache_key = f"document_{doc.id}_is_paused"
                    redis_client.delete(indexing_cache_key)
                except Exception:
                    pass
                
                # 重置所有段落状态为 waiting（可选，如果需要完全重新索引）
                # segments = db.session.query(DocumentSegment).filter(
                #     DocumentSegment.document_id == doc.id
                # ).all()
                # for segment in segments:
                #     segment.status = 'waiting'
                #     segment.indexing_at = None
                #     segment.completed_at = None
                
                db.session.add(doc)
                db.session.commit()
                
                print(f"  ✓ 文档状态已重置")
                
                # 触发重新索引 - 使用多种方式尝试
                task_submitted = False
                
                # 方法1: 尝试使用新的任务函数
                try:
                    from tasks.document_indexing_task import normal_document_indexing_task, priority_document_indexing_task
                    if use_priority:
                        task = priority_document_indexing_task.delay(
                            tenant_id=doc.tenant_id,
                            dataset_id=doc.dataset_id,
                            document_ids=[doc.id]
                        )
                        print(f"  ✓ 已提交优先索引任务: {task.id}")
                    else:
                        task = normal_document_indexing_task.delay(
                            tenant_id=doc.tenant_id,
                            dataset_id=doc.dataset_id,
                            document_ids=[doc.id]
                        )
                        print(f"  ✓ 已提交普通索引任务: {task.id}")
                    task_submitted = True
                except (ImportError, AttributeError) as e:
                    # 方法2: 使用旧的 document_indexing_task
                    try:
                        from tasks.document_indexing_task import document_indexing_task
                        task = document_indexing_task.delay(doc.dataset_id, [doc.id])
                        print(f"  ✓ 已提交索引任务（使用旧版 API）: {task.id}")
                        task_submitted = True
                    except Exception as e2:
                        # 方法3: 直接调用内部函数（同步执行，不推荐用于生产）
                        print(f"  ⚠️  Celery 任务不可用，使用同步方式执行索引")
                        try:
                            from tasks.document_indexing_task import _document_indexing
                            _document_indexing(doc.dataset_id, [doc.id])
                            print(f"  ✓ 索引任务已同步执行完成")
                            task_submitted = True
                        except Exception as e3:
                            print(f"  ✗ 无法执行索引任务: {e3}")
                
                if not task_submitted:
                    print(f"  ⚠️  警告：未能提交索引任务，请检查 Celery 配置")
                
            except Exception as e:
                print(f"  ✗ 错误：处理文档 {doc.id} 时出错: {e}")
                db.session.rollback()
                continue
        
        print(f"\n✓ 成功提交 {len(documents)} 个文档的重新索引任务")
        print("文档将在后台开始重新索引。")


if __name__ == "__main__":
    document_name_or_id = None
    use_priority = True
    
    if len(sys.argv) > 1:
        document_name_or_id = sys.argv[1]
    
    if len(sys.argv) > 2:
        use_priority = sys.argv[2].lower() in ['true', '1', 'yes', 'priority']
    
    rerun_document_indexing(document_name_or_id, use_priority)

