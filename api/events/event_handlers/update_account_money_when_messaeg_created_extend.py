from configs import dify_config
from events.message_event import message_was_created
from extensions.ext_database import db
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.api_token_money_extend import ApiTokenMessageJoinsExtend, ApiTokenMoneyExtend
from models.model_extend import EndUserAccountJoinsExtend


@message_was_created.connect
def handle(sender, **kwargs):
    message = sender
    if message.from_account_id is None and message.from_end_user_id is None:
        return

    payerId = message.from_end_user_id  # 付钱的ID
    if message.from_account_id is not None:
        payerId = message.from_account_id
    else:
        # web应用的请求，from_end_user_id记录的是登录账号的ID，可以拿这个ID来扣钱
        # API调用，from_end_user_id记录的是节点登录账号ID，
        # 真正需要扣钱的在关联表EndUserAccountJoinsExtend，需要多做一层查询
        account = db.session.query(Account).filter(Account.id == message.from_end_user_id).first()
        if not account:
            end_user_account_joins = (
                db.session.query(EndUserAccountJoinsExtend)
                .filter(EndUserAccountJoinsExtend.end_user_id == message.from_end_user_id)
                .order_by(EndUserAccountJoinsExtend.created_at.desc())
                .first()
            )
            if end_user_account_joins:
                payerId = end_user_account_joins.account_id

    account_money = db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == payerId).first()
    if message.currency == "USD":
        price = float(message.total_price)
    else:
        price = float(message.total_price) / float(dify_config.RMB_TO_USD_RATE)
    if account_money:
        db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == payerId).update(
            {"used_quota": float(account_money.used_quota) + price}
        )
    else:
        account_money_add = AccountMoneyExtend(
            account_id=payerId,
            used_quota=price,
            total_quota=dify_config.ACCOUNT_TOTAL_QUOTA,
        )
        db.session.add(account_money_add)

    # 扣掉密钥的钱
    api_token_message = (
        db.session.query(ApiTokenMessageJoinsExtend).filter(ApiTokenMessageJoinsExtend.record_id == message.id).first()
    )

    if api_token_message:
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
