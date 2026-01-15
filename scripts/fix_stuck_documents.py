#!/usr/bin/env python3
"""
修复卡住的文档：检查并修复那些已经索引完成但状态未更新的文档

用法:
    python scripts/fix_stuck_documents.py
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "api"))

from sqlalchemy import func, select
from app_factory import create_app
from extensions.ext_database import db
from libs.datetime_utils import naive_utc_now
from models.dataset import Document, DocumentSegment


def fix_stuck_documents():
    """修复卡住的文档：检查并更新状态"""
    
    # 创建 Flask 应用并初始化数据库连接
    app = create_app()
    
    with app.app_context():
        # 查找状态为 indexing 的文档
        indexing_documents = db.session.query(Document).filter(
            Document.indexing_status == 'indexing'
        ).all()
        
        if not indexing_documents:
            print("✓ 没有找到状态为 'indexing' 的文档。")
        else:
            print(f"找到 {len(indexing_documents)} 个状态为 'indexing' 的文档，开始检查...\n")
        
        fixed_docs = []
        stuck_docs = []
        
        for doc in indexing_documents:
            # 检查该文档的所有段落
            segments = db.session.query(DocumentSegment).filter(
                DocumentSegment.document_id == doc.id
            ).all()
            
            if not segments:
                # 没有段落，可能是异常情况
                print(f"  ⚠️  文档 {doc.id} ({doc.name[:50]}) 没有段落，跳过")
                continue
            
            # 统计段落状态
            total_segments = len(segments)
            completed_segments = sum(1 for s in segments if s.status == 'completed')
            indexing_segments = sum(1 for s in segments if s.status == 'indexing')
            waiting_segments = sum(1 for s in segments if s.status == 'waiting')
            
            # 检查是否所有段落都已完成
            if completed_segments == total_segments:
                # 所有段落都完成了，但文档状态还是 indexing，需要修复
                print(f"  ✓ 文档 {doc.id} ({doc.name[:50]})")
                print(f"     段落: {completed_segments}/{total_segments} 已完成")
                print(f"     状态: indexing -> completed")
                
                # 更新文档状态
                doc.indexing_status = 'completed'
                doc.completed_at = naive_utc_now()
                if doc.error:
                    doc.error = None
                
                # 计算统计信息
                total_tokens = sum(s.tokens for s in segments if s.tokens)
                if total_tokens:
                    doc.tokens = total_tokens
                
                fixed_docs.append(doc)
                
            elif indexing_segments > 0 or waiting_segments > 0:
                # 还有段落在处理中，但可能卡住了
                print(f"  ⚠️  文档 {doc.id} ({doc.name[:50]})")
                print(f"     段落状态: 已完成 {completed_segments}, 索引中 {indexing_segments}, 等待中 {waiting_segments}, 总计 {total_segments}")
                
                # 检查索引中的段落是否实际上已经完成了（有 index_node_id）
                indexing_segments_list = [s for s in segments if s.status == 'indexing']
                
                # 检查这些段落是否有 index_node_id（说明已经索引完成）
                # 对于大量段落，先采样检查
                if len(indexing_segments_list) > 1000:
                    sample_size = 1000
                    sample_segments = indexing_segments_list[:sample_size]
                    segments_with_node_id_in_sample = [s for s in sample_segments if s.index_node_id]
                    
                    if segments_with_node_id_in_sample:
                        print(f"     采样检查：{sample_size} 个段落中有 {len(segments_with_node_id_in_sample)} 个有索引节点")
                        print(f"     正在检查所有 {len(indexing_segments_list)} 个段落...")
                        # 检查所有段落
                        actually_completed_segments = [s for s in indexing_segments_list if s.index_node_id]
                        print(f"     ⚠️  发现 {len(actually_completed_segments)}/{indexing_segments} 个段落状态为'索引中'但已有索引节点（实际已完成）")
                        stuck_docs.append((doc, actually_completed_segments))
                    else:
                        print(f"     采样检查：{sample_size} 个段落中都没有索引节点")
                        # 检查是否超时
                        from datetime import timedelta
                        one_hour_ago = naive_utc_now() - timedelta(hours=1)
                        stuck_segments = [
                            s for s in indexing_segments_list 
                            if s.indexing_at and s.indexing_at < one_hour_ago
                        ]
                        if stuck_segments:
                            print(f"     发现 {len(stuck_segments)} 个卡住的段落（超过1小时）")
                            stuck_docs.append((doc, stuck_segments))
                else:
                    # 段落数量不多，直接检查所有
                    actually_completed_segments = [s for s in indexing_segments_list if s.index_node_id]
                    
                    if actually_completed_segments:
                        print(f"     ⚠️  发现 {len(actually_completed_segments)}/{indexing_segments} 个段落状态为'索引中'但已有索引节点（实际已完成）")
                        stuck_docs.append((doc, actually_completed_segments))
                    else:
                        # 检查是否超时
                        from datetime import timedelta
                        one_hour_ago = naive_utc_now() - timedelta(hours=1)
                        stuck_segments = [
                            s for s in indexing_segments_list 
                            if s.indexing_at and s.indexing_at < one_hour_ago
                        ]
                        if stuck_segments:
                            print(f"     发现 {len(stuck_segments)} 个卡住的段落（超过1小时）")
                            stuck_docs.append((doc, stuck_segments))
        
        # 处理卡住的段落
        confirmation = None
        if stuck_docs:
            print(f"\n发现 {len(stuck_docs)} 个文档有需要修复的段落")
            total_stuck_segments = sum(len(segments) for _, segments in stuck_docs)
            print(f"总共需要修复 {total_stuck_segments} 个段落")
            print("是否要将这些段落标记为已完成？(yes/no): ", end='')
            confirmation = input()
            
            if confirmation.lower() in ['yes', 'y']:
                fixed_segments_count = 0
                for doc, stuck_segments in stuck_docs:
                    print(f"\n处理文档 {doc.id} ({doc.name[:50]}) 的 {len(stuck_segments)} 个段落...")
                    for segment in stuck_segments:
                        # 检查段落是否真的已经索引了（有 index_node_id）
                        if segment.index_node_id:
                            segment.status = 'completed'
                            segment.completed_at = naive_utc_now()
                            segment.enabled = True
                            fixed_segments_count += 1
                            if fixed_segments_count <= 20:  # 只显示前20个
                                print(f"  ✓ 段落 {segment.id[:8]}... 标记为已完成（有索引节点）")
                            elif fixed_segments_count == 21:
                                print("  ... (更多段落正在更新)")
                        else:
                            # 没有索引节点，可能索引失败了
                            segment.status = 'error'
                            segment.error = '索引超时：段落处理超过1小时未完成'
                            fixed_segments_count += 1
                            if fixed_segments_count <= 20:
                                print(f"  ⚠️  段落 {segment.id[:8]}... 标记为错误（无索引节点）")
                
                print(f"\n已处理 {fixed_segments_count} 个段落，重新检查文档状态...")
                
                # 重新检查这些文档
                for doc, _ in stuck_docs:
                    # 重新查询以确保获取最新状态
                    db.session.refresh(doc)
                    segments = db.session.query(DocumentSegment).filter(
                        DocumentSegment.document_id == doc.id
                    ).all()
                    completed_count = sum(1 for s in segments if s.status == 'completed')
                    total_count = len(segments)
                    
                    print(f"  文档 {doc.id}: {completed_count}/{total_count} 段落已完成")
                    
                    if completed_count == total_count:
                        doc.indexing_status = 'completed'
                        doc.completed_at = naive_utc_now()
                        if doc.error:
                            doc.error = None
                        fixed_docs.append(doc)
                        print(f"  ✓ 文档 {doc.id} 现在可以标记为已完成")
                    elif completed_count > 0:
                        print(f"  ⚠️  文档 {doc.id} 还有 {total_count - completed_count} 个段落未完成")
        
        # 提交所有更改
        if fixed_docs or (stuck_docs and confirmation and confirmation.lower() in ['yes', 'y']):
            try:
                db.session.commit()
                print(f"\n✓ 成功修复 {len(fixed_docs)} 个文档的状态")
                if stuck_docs and confirmation.lower() in ['yes', 'y']:
                    print(f"✓ 修复了卡住的段落")
            except Exception as e:
                db.session.rollback()
                print(f"\n✗ 错误：提交更改时出错: {e}")
                sys.exit(1)
        else:
            print("\n没有需要修复的文档。")
        
        # 额外检查：查找状态为 paused 但段落都完成的文档
        print("\n" + "="*60)
        print("检查暂停状态但已完成的文档...")
        
        paused_documents = db.session.query(Document).filter(
            Document.is_paused == True,
            Document.indexing_status.in_(['waiting', 'parsing', 'cleaning', 'splitting', 'indexing'])
        ).all()
        
        if paused_documents:
            print(f"找到 {len(paused_documents)} 个暂停的文档，检查段落状态...")
            
            for doc in paused_documents:
                segments = db.session.query(DocumentSegment).filter(
                    DocumentSegment.document_id == doc.id
                ).all()
                
                if segments:
                    completed_count = sum(1 for s in segments if s.status == 'completed')
                    total_count = len(segments)
                    
                    if completed_count == total_count:
                        print(f"  ✓ 文档 {doc.id} ({doc.name[:50]}) 已暂停但段落都完成了")
                        print(f"     建议：可以取消暂停并标记为已完成")


if __name__ == "__main__":
    fix_stuck_documents()

