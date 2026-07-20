"""模型同步扩展服务（历史遗留，当前无存活调用方）。

原为 GVA 管理后台「同步所有模型到新工作区」功能的服务层。管理后台于 2026-07-05 退役后，
本模块与 services/model_service_extend.py 已无任何调用方；且其依赖的
``ProviderConfiguration.add_or_update_custom_*_without_validate_extend`` /
``get_custom_model_credentials`` / ``get_custom_credentials`` 方法在上游 model_runtime
外部化（graphon 化）后已不存在——若被调用会直接 AttributeError。1.16.0 合并复核时仅修复
import 路径使模块可导入；是否恢复功能或删除本模块，留待维护者决策（见
docs/dify-plus/复核记录-1.16.0-后端.md）。
"""

import logging

from core.provider_manager import ProviderManager
from extensions.ext_database import db
from graphon.model_runtime.entities.model_entities import ModelType
from models.tenant_model_sync_extend import TenantModelSyncExtend

logger = logging.getLogger(__name__)


class ModelProviderExtendService:
    """
    Model Provider Service
    """

    def __init__(self) -> None:
        self.provider_manager = ProviderManager()

    def get_model_credentials_obfuscated(self, tenant_id: str, provider: str, model_type: str, model: str) -> dict:
        """
        get model credentials.

        :param tenant_id: workspace id
        :param provider: provider name
        :param model_type: model type
        :param model: model name
        :return:
        """
        # Get all provider configurations of the current workspace
        provider_configurations = self.provider_manager.get_configurations(tenant_id)

        # Get provider configuration
        provider_configuration = provider_configurations.get(provider)
        if not provider_configuration:
            raise ValueError(f"Provider {provider} does not exist.")

        # Get model custom credentials from ProviderModel if exists
        return provider_configuration.get_custom_model_credentials(
            model_type=ModelType.value_of(model_type), model=model, obfuscated=False
        )

    @staticmethod
    def create_tenant_model_sync_if_not_exist(
        tenant_id: str, model_id, origin_model_id: str, is_all: bool = False
    ) -> bool:
        available_ta = (
            TenantModelSyncExtend.query.filter_by(
                tenant_id=tenant_id, model_id=model_id, origin_model_id=origin_model_id
            )
            .order_by(TenantModelSyncExtend.id.asc())
            .first()
        )

        if available_ta:
            return False

        ta = TenantModelSyncExtend(
            tenant_id=tenant_id, model_id=model_id, origin_model_id=origin_model_id, is_all=is_all
        )
        db.session.add(ta)
        db.session.commit()
        return True

    def save_model_credentials_without_validate(
        self, tenant_id: str, provider: str, model_type: str, model: str, credentials: dict
    ) -> str:
        """
        save model credentials.

        :param tenant_id: workspace id
        :param provider: provider name
        :param model_type: model type
        :param model: model name
        :param credentials: model credentials
        :return:
        """
        # Get all provider configurations of the current workspace
        provider_configurations = self.provider_manager.get_configurations(tenant_id)

        # Get provider configuration
        provider_configuration = provider_configurations.get(provider)
        if not provider_configuration:
            raise ValueError(f"Provider {provider} does not exist.")
        # Add or update custom model credentials
        return provider_configuration.add_or_update_custom_model_credentials_without_validate_extend(
            model_type=ModelType.value_of(model_type), model=model, credentials=credentials
        )

    def save_provider_credentials_without_validate(self, tenant_id: str, provider: str, credentials: dict) -> str:
        """
        save custom provider config.

        :param tenant_id: workspace id
        :param provider: provider name
        :param credentials: provider credentials
        :return:
        """
        # Get all provider configurations of the current workspace
        provider_configurations = self.provider_manager.get_configurations(tenant_id)

        # Get provider configuration
        provider_configuration = provider_configurations.get(provider)
        if not provider_configuration:
            raise ValueError(f"Provider {provider} does not exist.")

        # Add or update custom provider credentials.
        return provider_configuration.add_or_update_custom_credentials_without_validate_extend(credentials)

    def get_provider_credentials_obfuscated(self, tenant_id: str, provider: str) -> dict:
        """
        get provider credentials.

        :param tenant_id:
        :param provider:
        :return:
        """
        # Get all provider configurations of the current workspace
        provider_configurations = self.provider_manager.get_configurations(tenant_id)

        # Get provider configuration
        provider_configuration = provider_configurations.get(provider)
        if not provider_configuration:
            raise ValueError(f"Provider {provider} does not exist.")

        # Get provider custom credentials from workspace
        return provider_configuration.get_custom_credentials(obfuscated=False)

    @staticmethod
    def get_current_syned_tenants(origin_model_id: str) -> list[TenantModelSyncExtend]:
        return (
            db.session.query(TenantModelSyncExtend)
            .filter(TenantModelSyncExtend.origin_model_id == origin_model_id)
            .all()
        )

    @staticmethod
    def delete_syned_tenants(origin_model_id, tenant_id: str) -> bool:
        syned_tenant = (
            db.session.query(TenantModelSyncExtend)
            .filter(
                TenantModelSyncExtend.origin_model_id == origin_model_id,
                TenantModelSyncExtend.tenant_id == tenant_id,
            )
            .first()
        )

        db.session.delete(syned_tenant)
        db.session.commit()

        return True
