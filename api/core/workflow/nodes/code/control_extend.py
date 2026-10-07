"""code 节点 sandbox-full 授权判定（读侧热路径，p5-admin-decommission 收敛后的唯一读实现）。

历史上存在 core/workflow 与 dify_graph 双副本；1.14.2 合并删除了 dify_graph 包
（code 节点实现归属外部 graphon 包），故本模块收敛为唯一正本。

redis 键 ``control_mail`` 语义：JSON 邮箱数组（persistent 无 TTL），是数据库表
code_execution_control_extend 的投影缓存。唯一写入方是
services.system_manage_extend.CodeExecutionControlService（每次名单变更后全量重建）。

安全默认：键缺失、值非法 JSON、redis/DB 异常一律返回 False（回退普通 sandbox），
绝不抛异常中断 workflow 执行。

调用方：core.workflow.node_factory.DefaultWorkflowCodeExecutor（执行 code 节点时以
check_code 结果作为 purview 传入 CodeExecutor.execute_workflow_code_template）。
"""

import json
import logging

from sqlalchemy import and_
from sqlalchemy.exc import SQLAlchemyError

from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.account import Account, TenantAccountJoin

logger = logging.getLogger(__name__)


class ExecutionControl:
    """判定 tenant 是否在 sandbox-full 授权名单内（无状态，可随用随建）"""

    def check_code(self, tenant_id: str) -> bool:
        """判断 tenant 的 owner 邮箱是否命中 control_mail 授权名单。

        :param tenant_id: 目标 workspace id
        :return: True 表示 code 节点应提交到 FULL_CODE_EXECUTION_ENDPOINT（sandbox-full）；
            名单未命中或任何读取/解析/查询失败均返回 False（安全默认，走普通 sandbox）。
        """
        try:
            control_mail = redis_client.get("control_mail")
        except Exception:
            # redis 客户端异常类型因部署形态而异，广捕获以保证热路径绝不抛出
            logger.warning("Failed to read control_mail from redis; fallback to purview=False", exc_info=True)
            return False
        if not control_mail:
            return False

        try:
            control_rules = json.loads(control_mail)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            logger.warning("Invalid JSON in control_mail redis key; fallback to purview=False", exc_info=True)
            return False
        if not isinstance(control_rules, list) or not control_rules:
            return False

        try:
            matched = (
                db.session.query(Account)
                .join(TenantAccountJoin, Account.id == TenantAccountJoin.account_id)
                .filter(
                    and_(
                        TenantAccountJoin.tenant_id == tenant_id,
                        TenantAccountJoin.role == "owner",
                        Account.email.in_(control_rules),
                    )
                )
                .count()
            )
            return matched > 0
        except SQLAlchemyError:
            logger.warning(
                "Failed to query tenant owner for control_mail check (tenant %s); fallback to purview=False",
                tenant_id,
                exc_info=True,
            )
            return False
