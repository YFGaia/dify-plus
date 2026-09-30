from uuid import uuid4

from extensions.ext_database import db

from .types import StringUUID


class ApiTokenMoneyExtend(db.Model):
    __tablename__ = "api_token_money_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="api_token_money_extend_pkey"),
        db.Index("api_tokens_money_app_token_id_idx", "app_token_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_token_id = db.Column(StringUUID, nullable=True)  # 密钥ID
    accumulated_quota = db.Column(db.Numeric(16, 7))  # 已使用额度（累计不归零）
    day_used_quota = db.Column(db.Numeric(16, 7))  # 当天使用额度（定时脚本每日更新）
    month_used_quota = db.Column(db.Numeric(16, 7))  # 当月使用额度（定时脚本每月更新）
    day_limit_quota = db.Column(db.Numeric(16, 7))  # 每天使用额度限制（创建密钥时设置）
    month_limit_quota = db.Column(db.Numeric(16, 7))  # 每月使用额度限制（创建密钥时设置）
    description = db.Column(db.String(50))  # 密钥描述
    is_deleted = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


# 日快照统计表
class ApiTokenMoneyDailyStatExtend(db.Model):
    __tablename__ = "api_token_money_daily_stat_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="api_token_money_daily_stat_pkey"),
        db.Index("idx_api_token_money_daily_stat_app_token_id", "app_token_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_token_id = db.Column(StringUUID, nullable=False)
    accumulated_quota = db.Column(db.Numeric(16, 7))  # 已使用额度（累计不归零）
    day_used_quota = db.Column(db.Numeric(16, 7))  # 当天使用额度（定时脚本每日更新）
    day_limit_quota = db.Column(db.Numeric(16, 7))  # 每天使用额度限制（创建密钥时设置）
    stat_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


# 月快照统计表
class ApiTokenMoneyMonthlyStatExtend(db.Model):
    __tablename__ = "api_token_money_monthly_stat_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="api_token_money_monthly_stat_pkey"),
        db.Index("idx_api_token_money_monthly_stat_app_token_id", "app_token_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_token_id = db.Column(StringUUID, nullable=False)
    accumulated_quota = db.Column(db.Numeric(16, 7))  # 已使用额度（累计不归零）
    month_used_quota = db.Column(db.Numeric(16, 7))  # 当月使用额度（定时脚本每月更新）
    month_limit_quota = db.Column(db.Numeric(16, 7))  # 每月使用额度限制（创建密钥时设置）
    stat_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


# 密钥 - 对话消息关联表
class ApiTokenMessageJoinsExtend(db.Model):
    __tablename__ = "api_token_message_joins_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="api_token_message_joins_extend_pkey"),
        db.Index("api_token_message_joins_extend_app_token_id_idx", "app_token_id"),
        db.Index("api_token_message_joins_extend_record_id_idx", "record_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_token_id = db.Column(StringUUID, nullable=True)  # 密钥ID
    record_id = db.Column(StringUUID, nullable=True)  # 关联记录ID
    app_mode = db.Column(db.String(255), nullable=True)  # 应用类型
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))

    def add_app_token_record_id(self):
        db.session.add(
            ApiTokenMessageJoinsExtend(app_token_id=self.app_token_id, record_id=self.record_id, app_mode=self.app_mode)
        )
        db.session.commit()
