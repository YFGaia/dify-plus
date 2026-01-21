import json
import logging
from decimal import Decimal
from typing import Optional

import click
from celery import shared_task
from sqlalchemy import exists
from sqlalchemy.exc import SQLAlchemyError

from configs import dify_config
from core.workflow.enums import NodeType
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.api_token_money_extend import ApiTokenMessageJoinsExtend, ApiTokenMoneyExtend
from models.enums import CreatorUserRole
from models.model_extend import EndUserAccountJoinsExtend

# 缓存键前缀和过期时间
PAYER_ID_CACHE_PREFIX = "billing:payer_id:"
PAYER_ID_CACHE_TTL = 3600  # 1小时缓存


def _get_payer_id_from_cache(end_user_id: str) -> Optional[str]:
    """从Redis缓存获取付费人ID"""
    try:
        cache_key = f"{PAYER_ID_CACHE_PREFIX}{end_user_id}"
        cached_value = redis_client.get(cache_key)
        if cached_value:
            return cached_value.decode('utf-8') if isinstance(cached_value, bytes) else cached_value
    except Exception as e:
        logging.debug("缓存读取失败: %s", e)
    return None


def _set_payer_id_to_cache(end_user_id: str, payer_id: str) -> None:
    """将付费人ID写入Redis缓存"""
    try:
        cache_key = f"{PAYER_ID_CACHE_PREFIX}{end_user_id}"
        redis_client.setex(cache_key, PAYER_ID_CACHE_TTL, payer_id)
    except Exception as e:
        logging.debug("缓存写入失败: %s", e)


def _resolve_payer_id(created_by: str, created_by_role: Optional[str]) -> str:
    """
    解析实际付费人ID
    使用缓存+高效查询优化性能
    """
    payer_id = created_by
    
    if created_by_role != CreatorUserRole.END_USER.value:
        return payer_id
    
    # 先检查缓存
    cached_payer_id = _get_payer_id_from_cache(created_by)
    if cached_payer_id:
        return cached_payer_id
    
    # 使用 EXISTS 子查询检查是否是真实账户，比 SELECT 更高效
    is_account = db.session.query(
        exists().where(Account.id == created_by)
    ).scalar()
    
    if is_account:
        # 是真实账户，缓存并返回
        _set_payer_id_to_cache(created_by, created_by)
        return created_by
    
    # 查询关联表获取真正的付费账户
    # 只选择需要的字段，使用索引优化查询
    end_user_account = (
        db.session.query(EndUserAccountJoinsExtend.account_id)
        .filter(EndUserAccountJoinsExtend.end_user_id == created_by)
        .order_by(EndUserAccountJoinsExtend.created_at.desc())
        .first()
    )
    
    if end_user_account:
        payer_id = str(end_user_account.account_id)
    
    # 缓存结果
    _set_payer_id_to_cache(created_by, payer_id)
    return payer_id


@shared_task(queue="extend_high", bind=True, max_retries=3)
def update_account_money_when_workflow_node_execution_created_extend(
    self, workflow_node_execution_dict: dict):
    """
    计算工作流节点执行的费用并更新账户额度
    优化版本：使用缓存减少数据库查询，使用原子更新避免并发问题
    :param workflow_node_execution_dict: 工作流节点执行字典
    """

    if not workflow_node_execution_dict:
        logging.warning(click.style("工作流节点数据为空", fg="yellow"))
        return

    # 非大模型则跳过
    if workflow_node_execution_dict.get("node_type") != NodeType.LLM.value:
        return

    node_id = workflow_node_execution_dict.get("id")
    logging.info(click.style("工作流节点ID： {}".format(node_id), fg="cyan"))

    # 拿到费用 - 从 outputs 字段获取费用信息
    outputs = workflow_node_execution_dict.get("outputs", {})

    # 如果 outputs 是字符串，则解析 JSON；如果已经是字典，则直接使用
    if isinstance(outputs, str):
        outputs = json.loads(outputs) if outputs else {}
    elif not isinstance(outputs, dict):
        outputs = {}

    usage = outputs.get("usage", {})
    total_price = Decimal(usage.get("total_price", 0))
    currency = usage.get("currency", "USD")

    if total_price == 0:
        return
    price = float(total_price) if currency == "USD" else (
        float(total_price) / float(dify_config.RMB_TO_USD_RATE))
    logging.info(click.style("扣除费用： {}".format(price), fg="green"))

    try:
        created_by = workflow_node_execution_dict.get("created_by")
        created_by_role = workflow_node_execution_dict.get("created_by_role")
        
        # 使用优化后的方法获取付费人ID
        payer_id = _resolve_payer_id(created_by, created_by_role)
        logging.info(click.style("更新账号额度，账号ID： {}".format(payer_id), fg="green"))

        # 使用原子更新，避免并发问题，并减少一次查询
        # UPDATE ... SET used_quota = used_quota + price WHERE account_id = ?
        rows_updated = db.session.query(AccountMoneyExtend).filter(
            AccountMoneyExtend.account_id == payer_id
        ).update(
            {"used_quota": AccountMoneyExtend.used_quota + price},
            synchronize_session=False
        )
        
        if rows_updated == 0:
            # 记录不存在，创建新记录
            account_money_add = AccountMoneyExtend(
                account_id=payer_id,
                used_quota=price,
                total_quota=dify_config.ACCOUNT_TOTAL_QUOTA,
            )
            db.session.add(account_money_add)

        # 扣掉密钥的钱
        workflow_run_id = workflow_node_execution_dict.get("workflow_run_id")
        if workflow_run_id:
            # 只查询需要的字段
            api_token_id_result = (
                db.session.query(ApiTokenMessageJoinsExtend.app_token_id)
                .filter(ApiTokenMessageJoinsExtend.record_id == workflow_run_id)
                .first()
            )

            if api_token_id_result and api_token_id_result.app_token_id:
                app_token_id = api_token_id_result.app_token_id
                logging.info(click.style("更新密钥额度，密钥ID： {}".format(app_token_id), fg="green"))
                db.session.query(ApiTokenMoneyExtend).filter(
                    ApiTokenMoneyExtend.app_token_id == app_token_id
                ).update(
                    {
                        "accumulated_quota": ApiTokenMoneyExtend.accumulated_quota + price,
                        "day_used_quota": ApiTokenMoneyExtend.day_used_quota + price,
                        "month_used_quota": ApiTokenMoneyExtend.month_used_quota + price,
                    },
                    synchronize_session=False
                )

        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        logging.exception(
            click.style(f"工作流节点ID： {format(node_id)}，扣除费用："
                        f"{format(price)} 数据库异常，60秒后进行重试，", fg="red")
        )
        raise self.retry(exc=e, countdown=60)  # Retry after 60 seconds
    except Exception as e:
        db.session.rollback()
        logging.exception(
            click.style(f"工作流节点ID： {format(node_id)}，扣除费用："
                        f"{format(price)} 异常报错，60秒后进行重试，", fg="red")
        )
        raise self.retry(exc=e, countdown=60)
