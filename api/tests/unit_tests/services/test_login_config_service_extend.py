import json

from sqlalchemy.orm import Session

from enums import DeploymentEdition
from models.system_extend import SystemIntegrationExtend
from services.entities.feature_entities import LicenseModel, LicenseStatus, SystemFeatureModel
from services.login_config_service_extend import get_login_config_extend


def test_login_configuration_materializes_only_public_fields(sqlite_session: Session, config_overrides) -> None:
    config_overrides(RMB_TO_USD_RATE=7.5)
    sqlite_session.add_all(
        [
            SystemIntegrationExtend(
                id=1, classify=1, status=True, app_key="client", corp_id="corp", app_secret="secret"
            ),
            SystemIntegrationExtend(
                id=2,
                classify=4,
                status=True,
                config=json.dumps({"server_url": "https://auth.example", "logout_url": "/logout", "secret": "private"}),
            ),
            SystemIntegrationExtend(id=3, classify=1, status=False, app_key="disabled"),
        ]
    )
    sqlite_session.commit()
    snapshot = SystemFeatureModel(
        deployment_edition=DeploymentEdition.COMMUNITY,
        license=LicenseModel(status=LicenseStatus.ACTIVE, expired_at="2099-01-01"),
    )
    result = get_login_config_extend(snapshot, session=sqlite_session)
    sqlite_session.expunge_all()
    sqlite_session.close()
    payload = result.model_dump(mode="json")
    assert payload["is_custom_auth2"] is True
    assert payload["is_custom_auth2_logout"] == "https://auth.example/logout"
    assert payload["ding_talk_client_id"] == "client"
    assert payload["ding_talk_corp_id"] == "corp"
    assert payload["rmb_to_usd_rate"] == 7.5
    assert payload["license"] == {"status": "active"}
    assert "secret" not in json.dumps(payload)
    assert "is_custom_auth2" not in snapshot.model_dump()


def test_empty_integrations_have_safe_defaults(sqlite_session: Session) -> None:
    snapshot = SystemFeatureModel(deployment_edition=DeploymentEdition.COMMUNITY)
    result = get_login_config_extend(snapshot, session=sqlite_session)
    assert result.ding_talk is False
    assert result.is_custom_auth2 is False
    assert result.ding_talk_client_id == ""
    assert result.is_custom_auth2_logout == ""
