"""Fresh independent checks for the C2 LOCAL choreography's failure edges."""

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.gateway import GatewayError
from enums import DeploymentEdition
from models.casdoor_extend import CasdoorIntegrationExtend
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from services.casdoor_local_login_coordinator_service_extend import CasdoorLocalLoginCoordinatorService
from test_casdoor_local_login_coordinator_service_extend import rows

pytest_plugins = ["test_casdoor_local_login_coordinator_service_extend"]


def test_fresh_loader_rejects_remote_disable_under_complete_leases(chain):
    """Fresh online status is revalidated while the actual full lease set is held."""

    before = rows(chain)

    def provider(path, count):
        if path == "/api/get-user" and count == 2:
            chain.user["isForbidden"] = True

    chain.control.hook = provider

    with pytest.raises(GatewayError) as error:
        chain.invoke()

    assert error.value.reason == "online_disabled"
    assert error.value.local_outcome == "not_started"
    assert error.value.cleanup_released is True
    assert chain.calls == [
        "/api/login/oauth/access_token",
        "/api/userinfo",
        "/api/get-user",
        "/api/userinfo",
        "/api/get-organization",
        "/api/get-user",
    ]
    assert rows(chain) == before
    assert len(chain.prepared) == 1 and not chain.prepared[0]._consumed
    assert not chain.redis.data


@pytest.mark.parametrize("state", ["disabled", "missing_active"])
def test_production_factory_rejects_inactive_database_configuration_without_external_io(chain, monkeypatch, state):
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    with chain.local.session.begin():
        integration = chain.local.session.scalar(sa.select(CasdoorIntegrationExtend))
        if state == "disabled":
            integration.enabled = False
        else:
            integration.active_revision_id = None

    class Deny:
        def __getattribute__(self, name):
            pytest.fail("inactive configuration accessed account/Redis side effects")

    deny = Deny()
    with pytest.raises(CasdoorConfigurationError) as error:
        CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=chain.coordinator._session_factory,
            configuration_service=chain.configuration_service,
            account_owner=deny,
            redis_client=deny,
        )

    assert error.value.reason == "inactive"
    assert not chain.calls
    assert not chain.prepared
    assert not chain.redis.data
    with pytest.raises(TypeError):
        CasdoorLocalLoginCoordinatorService.for_production(
            session_factory=deny,
            configuration_service=deny,
            account_owner=deny,
            redis_client=deny,
            directory_contract=chain.directory,
        )


def test_cleanup_cancellation_preserves_primary_provider_failure(chain):
    """A cleanup cancellation must not replace the actual provider failure."""

    class CleanupCancellation(BaseException):
        pass

    before = rows(chain)

    def provider(path, count):
        if path == "/api/get-organization":
            chain.organization.pop("accountItems")

    def redis(command, key):
        if command == "before_release":
            raise CleanupCancellation()

    chain.control.hook = provider
    chain.redis.hook = redis

    with pytest.raises(GatewayError) as error:
        chain.invoke()

    assert error.value.reason == "visibility_unknown"
    assert error.value.local_outcome == "not_started"
    assert error.value.cleanup_released is False
    assert rows(chain) == before
    assert len(chain.prepared) == 1 and not chain.prepared[0]._consumed
    assert chain.redis.data
