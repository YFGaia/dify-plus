import json
import logging
from decimal import Decimal

import click
from celery import shared_task
from sqlalchemy.exc import SQLAlchemyError

from configs import dify_config
from core.workflow.nodes.enums import NodeType
from extensions.ext_database import db
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.api_token_money_extend import ApiTokenMessageJoinsExtend, ApiTokenMoneyExtend
from models.enums import CreatorUserRole
from models.model_extend import EndUserAccountJoinsExtend


@shared_task(queue="extend_high", bind=True, max_retries=3)
def update_account_money_when_workflow_node_execution_created_extend(
    self, workflow_node_execution_dict: dict):
    """
    计算工作流节点执行的费用并更新账户额度
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

    # 拿到费用 - 从 outputs 字段获取费用信息（参考原始代码）
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
        # 当前是end_user，节点账号id
        # 分两种情况
        # web应用的请求，created_by记录的是登录账号的ID，可以拿这个ID来扣钱
        # API调用，created_by记录的是节点登录账号ID，真正需要扣钱的在关联表EndUserAccountJoinsExtend，需要多做一层查询
        created_by = workflow_node_execution_dict.get("created_by")
        created_by_role = workflow_node_execution_dict.get("created_by_role")
        payerId = created_by  # 付钱的ID
        if created_by_role == CreatorUserRole.END_USER.value:
            account = db.session.query(Account).filter(Account.id == created_by).first()
            if not account:
                end_user_account_joins = (
                    db.session.query(EndUserAccountJoinsExtend)
                    .filter(EndUserAccountJoinsExtend.end_user_id == created_by)
                    .order_by(EndUserAccountJoinsExtend.created_at.desc())
                    .first()
                )
                if end_user_account_joins:
                    payerId = end_user_account_joins.account_id

        account_money = db.session.query(AccountMoneyExtend).filter(
            AccountMoneyExtend.account_id == payerId).first()
        logging.info(click.style("更新账号额度，账号ID： {}".format(payerId), fg="green"))
        if account_money:
            db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == payerId).update(
            {
                "used_quota": float(account_money.used_quota) + price}
            )
        else:
            account_money_add = AccountMoneyExtend(
                account_id=payerId,
                used_quota=price,
                total_quota=dify_config.ACCOUNT_TOTAL_QUOTA,
            )
            db.session.add(account_money_add)

        # 扣掉密钥的钱
        workflow_run_id = workflow_node_execution_dict.get("workflow_run_id")
        api_token_message = (
            db.session.query(ApiTokenMessageJoinsExtend)
            .filter(ApiTokenMessageJoinsExtend.record_id == workflow_run_id)
            .first()
        )

        if api_token_message:
            logging.info(click.style("更新密钥额度，密钥ID： {}".format(
                api_token_message.app_token_id), fg="green"))
            db.session.query(ApiTokenMoneyExtend).filter(
                ApiTokenMoneyExtend.app_token_id == api_token_message.app_token_id
            ).update(
                {
                    "accumulated_quota": ApiTokenMoneyExtend.accumulated_quota + price,
                    "day_used_quota": ApiTokenMoneyExtend.day_used_quota + price,
                    "month_used_quota": ApiTokenMoneyExtend.month_used_quota + price,
                },
            )

        db.session.commit()
    except SQLAlchemyError as e:
        logging.exception(
            click.style(f"工作流节点ID： {format(node_id)}，扣除费用："
                        f"{format(price)} 数据库异常，60秒后进行重试，", fg="red")
        )
        raise self.retry(exc=e, countdown=60)  # Retry after 60 seconds
    except Exception as e:
        logging.exception(
            click.style(f"工作流节点ID： {format(node_id)}，扣除费用："
                        f"{format(price)} 异常报错，60秒后进行重试，", fg="red")
        )
        raise self.retry(exc=e, countdown=60)
