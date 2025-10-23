from extensions.ext_database import db

from .types import StringUUID


class AccountMoneyExtend(db.Model):
    __tablename__ = "account_money_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="account_money_pkey"),
        db.UniqueConstraint("account_id", name="idx_account_money_account_id_unique"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    account_id = db.Column(StringUUID, nullable=False)
    total_quota = db.Column(db.Numeric(16, 7))
    used_quota = db.Column(db.Numeric(16, 7))
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))


class AccountLayoverRecordExtend(db.Model):
    __tablename__ = "account_layover_record_extend"
    __table_args__ = (
        db.PrimaryKeyConstraint("id", name="account_layover_record_extend_pkey"),
        db.Index("idx_account_layover_record_account_id", "account_id"),
        db.Index("idx_account_layover_record_forwarding_id", "forwarding_id"),
    )

    id = db.Column(StringUUID, server_default=db.text("uuid_generate_v4()"))
    account_id = db.Column(StringUUID, nullable=False)
    forwarding_id = db.Column(StringUUID, nullable=False)
    money = db.Column(db.Numeric(16, 7))
    info = db.Column(db.JSON, default={})
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.text("CURRENT_TIMESTAMP(0)"))
