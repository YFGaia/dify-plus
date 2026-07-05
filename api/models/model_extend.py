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

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    end_user_id = db.Column(StringUUID, nullable=False)
    account_id = db.Column(StringUUID, nullable=False)
    app_id = db.Column(StringUUID, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


# Extend: 记忆上下文功能
class AppExtend(db.Model):
    __tablename__ = "app_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="app_extend_joins_pkey"),
        db.Index("app_extend_id_app_id_idx", "app_id"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    app_id = db.Column(StringUUID, nullable=False)
    retention_number = db.Column(db.Integer, nullable=True)


# Extend: 记忆上下文功能


# Extend: 消息上下文分割功能
class MessageContextExtend(db.Model):
    __tablename__ = "message_context_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="message_context_extend_joins_pkey"),
        db.Index("message_context_conversation_id_idx", "conversation_id"),
        db.Index("message_context_created_at_idx", "created_at"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
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

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    app_id = db.Column(StringUUID, nullable=False)
    number = db.Column(db.Integer, nullable=False, default=0)


# Extend: 应用中心 - 自建分类表（自 models/model.py 外迁，DEC-4；原生化迁移属 p3）
class RecommendedCategoryExtend(db.Model):
    __tablename__ = "recommended_category_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="category_extend_id_pkey"),
        db.Index("idx_extend_tag_bind_tag_id", "tag_id"),
        db.Index("idx_extend_table", "table"),
    )

    id = db.Column(StringUUID, primary_key=True, server_default=db.text("uuid_generate_v4()"))
    table = db.Column(db.String(255), nullable=False)
    tag_id = db.Column(StringUUID, nullable=True)


class RecommendedAppsCategoryJoinExtend(db.Model):
    __tablename__ = "recommended_apps_category_join_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="recommended_apps_category_id_pkey"),
        db.Index("idx_recommended_id", "recommended_id"),
        db.Index("idx_recommended_category_id", "category_id"),
    )

    id = db.Column(StringUUID, primary_key=True, server_default=db.text("uuid_generate_v4()"))
    recommended_id = db.Column(StringUUID, nullable=False)
    category_id = db.Column(StringUUID, nullable=False)
