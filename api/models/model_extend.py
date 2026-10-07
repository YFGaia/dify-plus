from uuid import uuid4

from extensions.ext_database import db

from .types import StringUUID


class EndUserAccountJoinsExtend(db.Model):
    __tablename__ = "end_user_account_joins_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="end_user_account_joins_pkey"),
        db.Index("end_user_account_joins_account_id_idx", "account_id"),
        db.Index("end_user_account_joins_end_user_id_idx", "end_user_id"),  # 单独索引，用于计费查询优化
        db.Index("end_user_account_joins_end_user_id_app_id_idx", "end_user_id", "app_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    end_user_id = db.Column(StringUUID, nullable=False)
    account_id = db.Column(StringUUID, nullable=False)
    app_id = db.Column(StringUUID, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


# Extend: per-app 扩展配置（记忆上下文 + WebApp 认证开关）
class AppExtend(db.Model):
    __tablename__ = "app_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="app_extend_joins_pkey"),
        db.Index("app_extend_id_app_id_idx", "app_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_id = db.Column(StringUUID, nullable=False)
    retention_number = db.Column(db.Integer, nullable=True)
    # WebApp 访问认证开关：NULL/True = 访问需登录 Console（fork 默认行为），False = 允许匿名访问。
    # 读写经 services.webapp_auth_service_extend（redis 投影缓存），不要绕过该服务直接改列。
    webapp_auth_enabled = db.Column(db.Boolean, nullable=True)


# Extend: per-app 扩展配置


# Extend: 消息上下文分割功能
class MessageContextExtend(db.Model):
    __tablename__ = "message_context_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="message_context_extend_joins_pkey"),
        db.Index("message_context_conversation_id_idx", "conversation_id"),
        db.Index("message_context_created_at_idx", "created_at"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    conversation_id = db.Column(db.String(36), nullable=True)
    message_id = db.Column(db.String(36), nullable=False)


# Extend: 消息上下文分割功能


# Extend: 应用中心 - 应用使用频次统计（自 models/model.py 外迁，DEC-4）
class AppStatisticsExtend(db.Model):
    __tablename__ = "app_statistics_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="app_statistics_extend_pkey"),
        db.Index("app_statistics_extend_app_id_idx", "app_id"),
    )

    id = db.Column(StringUUID, default=lambda: str(uuid4()), server_default=db.text("uuid_generate_v4()"))
    app_id = db.Column(StringUUID, nullable=False)
    number = db.Column(db.Integer, nullable=False, default=0)


# 注：fork 自建分类表 RecommendedCategoryExtend / RecommendedAppsCategoryJoinExtend 已退役（DD3，
# openspec change p3-merge-upstream-1-15-0）：分类数据经 migrations_extend 017 迁入
# recommended_apps.categories，两表由 018 drop 迁移删除，模型类随之移除。
