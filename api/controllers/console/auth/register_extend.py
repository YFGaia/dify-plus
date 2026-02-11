import uuid
import jwt
from flask import request
from .. import console_ns
from models import Account
from configs import dify_config
from datetime import UTC, datetime
from libs.login import login_required
from extensions.ext_database import db
from models.account import AccountStatus
from flask_restx import Resource, reqparse
from models.account_money_extend import AccountMoneyExtend
from services.account_service import AccountService, TenantService
from services.account_service_extend import TenantExtendService


@console_ns.route("/admin_register_user")
class AdminRegisterApi(Resource):
    """Resource for user login."""
    @login_required
    def post(self):
        """Authenticate user and login."""
        parser = reqparse.RequestParser()
        auth_header = request.headers.get("Authorization")
        parser.add_argument("name", type=str, required=True, location="json")
        parser.add_argument("nick", type=str, required=True, location="json")
        parser.add_argument("email", type=str, required=True, location="json")
        args = parser.parse_args()
        auth_scheme, auth_token = auth_header.split(None, 1)
        auth_scheme = auth_scheme.lower()
        if auth_scheme == "bearer":
            auth_header = auth_token
        decoded_jwt = jwt.decode(
            auth_header, dify_config.SECRET_KEY.encode(), algorithms=["HS256"], options={"verify_signature": False})
        # 解析jwt
        if not ("AuthorityId" in decoded_jwt.keys() and int(
                decoded_jwt["AuthorityId"]) == int(dify_config.ADMIN_GROUP_ID)):
            return {"error": "Unable to add a new backend super admin groups."}, 351
        try:
            if db.session.query(Account).filter_by(email=args.email).first() is not None:
                return {"error": "User already exists"}, 352
        except:
            return {"error": "Can't find the relevant user email."}, 353
        account = AccountService.create_account(
            password=str(uuid.uuid4()).replace('-', ''),
            interface_language="zh-Hans",
            email=args.email,
            name=args.name,
            is_setup=True,
        )

        # extend begin：初始化用户额度数据
        account_money_add = AccountMoneyExtend(
            account_id=account.id,
            used_quota=0,
            total_quota=dify_config.ACCOUNT_TOTAL_QUOTA,
        )
        db.session.add(account_money_add)
        # extend end：初始化用户额度数据

        account.last_login_ip = ""
        account.status = AccountStatus.ACTIVE.value
        account.initialized_at = datetime.now(UTC).replace(tzinfo=None)
        db.session.commit()

        # -------------- 二开部分 Begin - 邀请进第一个工作区 --------------
        tenant_extend_service = TenantExtendService
        super_admin_id = tenant_extend_service.get_super_admin_id().id
        super_admin_tenant_id = tenant_extend_service.get_super_admin_tenant_id().id
        if super_admin_id and super_admin_tenant_id:
            isCreate = TenantExtendService.create_default_tenant_member_if_not_exist(
                super_admin_tenant_id, account.id
            )  # 创建默认空间和用户的关系
            if isCreate:
                TenantService.switch_tenant(account, super_admin_tenant_id)

        return {"result": "success", "data": "ok"}


