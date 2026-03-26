import json
from abc import abstractmethod

from sqlalchemy import and_

from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.account import Account, TenantAccountJoin


class ExecutionControl:

    @abstractmethod
    def check_code(self, tenant_id: str):
        """
        检查代码是否符合用户的执行控制规则
        """
        # 获取tenant下拥有owner或admin权限的用户
        control_mail = redis_client.get("control_mail")
        if not control_mail:
            return False
        # 解析控制规则
        control_rules = json.loads(control_mail)
        # 构建查询
        try:
            query = (
                db.session.query(Account)
                .join(TenantAccountJoin, Account.id == TenantAccountJoin.account_id)
                .filter(
                    and_(
                        TenantAccountJoin.tenant_id == tenant_id,
                        TenantAccountJoin.role == "owner",
                        Account.email.in_(control_rules)
                    )
                ).all()
            )
            return len(query) > 0
        except:
            return False
