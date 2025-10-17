from flask_login import current_user
from flask_restx import Resource, marshal_with

from configs import dify_config
from controllers.console import api
from controllers.console.workspace.workspace import account_initialization_required, setup_required
from extensions.ext_database import db
from fields.member_fields_extend import account_money_fields
from libs.login import login_required
from models.account_money_extend import AccountMoneyExtend


class AccountMoneyApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @marshal_with(account_money_fields)
    def get(self):
        account_money = (
            db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == current_user.id).first()
        )
        if not account_money:
            return {"total_quota": dify_config.ACCOUNT_TOTAL_QUOTA, "used_quota": "0"}, 200
        return account_money


# Register API resources
api.add_resource(AccountMoneyApi, "/account/money")
