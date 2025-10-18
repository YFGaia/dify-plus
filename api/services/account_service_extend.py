from sqlalchemy import or_

from extensions.ext_database import db
from models.account import *
from models.account import TenantAccountJoin
from models.provider import Provider, ProviderModel
from models.tenant_model_sync_extend import ModelSyncConfigExtend, TenantModelSyncExtend


class TenantExtendService:
    @staticmethod
    def create_default_tenant_member_if_not_exist(tenant_id: str, account_id: str, role: str = "normal") -> bool:
        available_ta = (
            db.session.query(TenantAccountJoin).filter_by(account_id=account_id, tenant_id=tenant_id)
            .order_by(TenantAccountJoin.id.asc())
            .first()
        )

        if available_ta:
            return False

        ta = TenantAccountJoin(tenant_id=tenant_id, account_id=account_id, role=role, current=True)
        db.session.add(ta)
        db.session.commit()
        return True

    @staticmethod
    def get_all_tenants() -> list[Tenant]:
        """Get all tenants"""
        return db.session.query(Tenant).filter(Tenant.status == TenantStatus.NORMAL).all()

    @staticmethod
    def create_model_sync_config_if_not_exist(model_id: str, is_all: bool = True) -> bool:
        available_ta = (
            db.session.query(ModelSyncConfigExtend).filter_by(model_id=model_id).order_by(ModelSyncConfigExtend.id.asc()).first()
        )

        if available_ta:
            return False

        ta = ModelSyncConfigExtend(model_id=model_id, is_all=is_all)
        db.session.add(ta)
        db.session.commit()
        return True

    @staticmethod
    def get_sync_all_model() -> list[ProviderModel]:
        return (
            db.session.query(ProviderModel)
            .join(ModelSyncConfigExtend, ProviderModel.id == ModelSyncConfigExtend.model_id)
            .filter(ModelSyncConfigExtend.is_all == True)
            .all()
        )

    @staticmethod
    def get_sync_all_provider() -> list[Provider]:
        return (
            db.session.query(Provider)
            .join(ModelSyncConfigExtend, Provider.id == ModelSyncConfigExtend.model_id)
            .filter(ModelSyncConfigExtend.is_all == True)
            .all()
        )

    @staticmethod
    def get_model_sync_join_tenants(origin_model_id, current_role, account_id: str) -> list[Tenant]:
        """Get model sync join tenants"""
        if current_role == TenantAccountRole.OWNER:
            return (
                db.session.query(Tenant)
                .join(TenantModelSyncExtend, Tenant.id == TenantModelSyncExtend.tenant_id)
                .filter(TenantModelSyncExtend.origin_model_id == origin_model_id, Tenant.status == TenantStatus.NORMAL)
                .all()
            )
        else:
            # TODO 这里联合查询了 3 张表，可能后期数据量大，有数据查询瓶颈
            return (
                db.session.query(Tenant)
                .join(TenantModelSyncExtend, Tenant.id == TenantModelSyncExtend.tenant_id)
                .join(TenantAccountJoin, Tenant.id == TenantAccountJoin.tenant_id)
                .filter(
                    TenantAccountJoin.account_id == account_id,
                    TenantModelSyncExtend.origin_model_id == origin_model_id,
                    Tenant.status == TenantStatus.NORMAL,
                    or_(
                        TenantAccountJoin.role == TenantAccountRole.OWNER,
                        TenantAccountJoin.role == TenantAccountRole.ADMIN,
                    ),
                )
                .all()
            )

    @staticmethod
    def create_provider_sync_config_if_not_exist(provider_id: str, is_all: bool = True) -> bool:
        available_ta = (
            db.session.query(ModelSyncConfigExtend).filter_by(model_id=provider_id).order_by(ModelSyncConfigExtend.id.asc()).first()
        )

        if available_ta:
            return False

        ta = ModelSyncConfigExtend(model_id=provider_id, is_all=is_all)
        db.session.add(ta)
        db.session.commit()
        return True

    @staticmethod
    def get_provider_sync_join_tenants(origin_provider_id, current_role, account_id: str) -> list[Tenant]:
        """Get model sync join tenants"""
        if current_role == TenantAccountRole.OWNER:
            return (
                db.session.query(Tenant)
                .join(TenantModelSyncExtend, Tenant.id == TenantModelSyncExtend.tenant_id)
                .filter(
                    TenantModelSyncExtend.origin_model_id == origin_provider_id, Tenant.status == TenantStatus.NORMAL
                )
                .all()
            )
        else:
            # TODO 这里联合查询了 3 张表，可能后期数据量大，有数据查询瓶颈
            return (
                db.session.query(Tenant)
                .join(TenantModelSyncExtend, Tenant.id == TenantModelSyncExtend.tenant_id)
                .join(TenantAccountJoin, Tenant.id == TenantAccountJoin.tenant_id)
                .filter(
                    TenantAccountJoin.account_id == account_id,
                    TenantModelSyncExtend.origin_model_id == origin_provider_id,
                    Tenant.status == TenantStatus.NORMAL,
                    or_(
                        TenantAccountJoin.role == TenantAccountRole.OWNER,
                        TenantAccountJoin.role == TenantAccountRole.ADMIN,
                    ),
                )
                .all()
            )

    @staticmethod
    def delete_model_sync_config(model_id: str) -> bool:

        model_sync_record = db.session.query(ModelSyncConfigExtend).filter(ModelSyncConfigExtend.model_id == model_id).first()

        if model_sync_record is None:
            return True

        db.session.delete(model_sync_record)
        db.session.commit()

        return True

    @staticmethod
    def get_super_admin_tenant_id() -> Tenant:
        """第一个先创建的工作区作为默认空间"""
        # SELECT * FROM "public"."tenants" ORDER BY "created_at" ASC LIMIT 1
        return db.session.query(Tenant).order_by(Tenant.created_at.asc()).first()

    @staticmethod
    def get_super_admin_id() -> Account:
        """第一个先创建的用户作为超级管理员"""
        # SELECT * FROM "public"."tenants" ORDER BY "created_at" ASC LIMIT 1
        return db.session.query(Account).order_by(Account.created_at.asc()).first()