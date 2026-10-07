"""Fork Console login configuration, separate from the public system-feature allowlist."""

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from configs import dify_config
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend
from services.entities.feature_entities import SystemFeatureModel


class LoginConfigModelExtend(SystemFeatureModel):
    is_custom_auth2: bool = False
    is_custom_auth2_logout: str = ""
    ding_talk_client_id: str = ""
    ding_talk_corp_id: str = ""
    ding_talk: bool = False
    rmb_to_usd_rate: float = 7.26


class _OAuthLogoutConfig(BaseModel):
    server_url: str = ""
    logout_url: str = ""


def get_login_config_extend(public_features: SystemFeatureModel, *, session: Session) -> LoginConfigModelExtend:
    """Materialize only login-visible integration fields while the supplied session is open."""
    result = LoginConfigModelExtend.model_validate(public_features.model_dump())
    if dify_config.RMB_TO_USD_RATE is not None:
        result.rmb_to_usd_rate = float(dify_config.RMB_TO_USD_RATE)
    integrations = session.scalars(
        select(SystemIntegrationExtend)
        .where(SystemIntegrationExtend.status.is_(True))
        .order_by(SystemIntegrationExtend.id)
    )
    for integration in integrations:
        if integration.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK:
            result.ding_talk = True
            result.ding_talk_client_id = integration.app_key or ""
            result.ding_talk_corp_id = integration.corp_id or ""
        elif integration.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_OAUTH_TWO:
            config = _OAuthLogoutConfig.model_validate_json(integration.config or "{}")
            result.is_custom_auth2 = True
            result.is_custom_auth2_logout = config.server_url + config.logout_url if config.logout_url else ""
    return result
