from extensions.ext_database import db

from .types import StringUUID


class TenantModelSyncExtend(db.Model):
    """
    模型-工作区同步关联
    """

    __tablename__ = "tenant_model_sync_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="tenant_model_sync_extend_pkey"),
        db.Index("tenant_model_sync_extend_tenant_idx", "tenant_id"),
        db.Index("tenant_model_sync_extend_model_idx", "model_id"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    tenant_id = db.Column(StringUUID, nullable=False)
    model_id = db.Column(StringUUID, nullable=False)
    origin_model_id = db.Column(db.String(255), nullable=False)
    is_all = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))

    def __repr__(self):
        return (
            f"<tenant_model_sync(id={self.id}, tenant_id={self.tenant_id}, "
            f"model_id='{self.model_id}', is_all='{self.is_all}')>"
        )


class ModelSyncConfigExtend(db.Model):
    """
    模型-同步配置表
    """

    __tablename__ = "model_sync_config_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="model_sync_config_extend_pkey"),
        db.UniqueConstraint("model_id", name="unique_model_id"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    model_id = db.Column(StringUUID, nullable=True)
    is_all = db.Column(db.Boolean, nullable=True, server_default=db.text("true"))

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))

    def __repr__(self):
        return f"<model_sync_config_extend(id={self.id}, model_id='{self.model_id}', is_all='{self.is_all}')>"
