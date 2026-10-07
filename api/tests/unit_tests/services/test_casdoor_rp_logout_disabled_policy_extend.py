"""Exit-only historical policy semantics; real SQL/signed owner, synthetic inputs."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from core.casdoor.deployment_evidence import DeploymentEvidenceError
from models.casdoor_extend import (
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationError,
)
from services.casdoor_rp_logout_service_extend import RPLogoutUnavailable
from test_casdoor_diagnostic_flow_extend import send

pytest_plugins = (
    "test_casdoor_rp_logout_service_extend",
    "test_casdoor_diagnostic_flow_extend",
    "test_casdoor_rp_logout_mounted_extend",
)


def disabled(m):
    d = m.d
    snapshot = d.services.casdoor_configuration.get(d.actor)
    result = d.services.casdoor_configuration.disable(d.actor, etag=snapshot.etag)
    assert result.configuration.enabled is False
    with d.f.service._session_factory() as session:
        namespace = session.get(CasdoorNamespaceExtend, m.rp.binding.namespace_id)
        assert namespace.lifecycle is CasdoorNamespaceLifecycle.FENCING
        assert namespace.fence_epoch > 0
        epoch = namespace.fence_epoch
    return epoch


def test_disabled_reader_preserves_exact_policy_and_does_not_unfence_producers(mounted):
    m, d = mounted, mounted.d
    accepted = m.service._reviewed(m.rp.binding)
    epoch = disabled(m)
    current = m.service._reviewed(m.rp.binding)
    assert current.binding == accepted.binding
    assert current.proof_fingerprint == accepted.proof_fingerprint
    assert current.rp_logout == accepted.rp_logout
    assert current.expires_at == accepted.expires_at
    with d.f.service._session_factory() as session:
        namespace = session.get(CasdoorNamespaceExtend, m.rp.binding.namespace_id)
        assert namespace.lifecycle is CasdoorNamespaceLifecycle.FENCING
        assert namespace.fence_epoch == epoch
        assert session.get(CasdoorIntegrationExtend, namespace.integration_id).enabled is False
    with pytest.raises(CasdoorConfigurationError) as diagnostic_error:
        d.services.casdoor_diagnostic._base(UUID(m.rp.binding.revision_id))
    assert diagnostic_error.value.reason == "namespace_fenced"
    before = tuple(d.f.control.operations)
    login = send(d, "/console/api/auth/casdoor/login")
    assert login.status_code != 302
    assert tuple(d.f.control.operations) == before
    with pytest.raises(CasdoorConfigurationError):
        d.services.casdoor_configuration.activate(
            d.actor,
            etag=d.services.casdoor_configuration.get(d.actor).etag,
            revision_id=UUID(m.rp.binding.revision_id),
        )


@pytest.mark.parametrize(
    "failure",
    [
        "enabled_fencing",
        "archived",
        "missing_namespace",
        "wrong_revision",
        "wrong_core",
        "wrong_db_digest",
        "wrong_public_digest",
        "revoked",
        "expired",
        "missing_profile",
    ],
)
def test_disabled_historical_lookup_stays_closed(mounted, failure):
    m, d = mounted, mounted.d
    epoch = disabled(m)
    binding = m.rp.binding
    if failure in {"enabled_fencing", "archived"}:
        with d.f.service._session_factory() as session, session.begin():
            namespace = session.get(CasdoorNamespaceExtend, binding.namespace_id)
            if failure == "enabled_fencing":
                session.get(CasdoorIntegrationExtend, namespace.integration_id).enabled = True
            else:
                namespace.lifecycle = CasdoorNamespaceLifecycle.ARCHIVED
    elif failure == "missing_namespace":
        binding = binding.model_copy(update={"namespace_id": str(uuid4())})
    elif failure == "wrong_revision":
        binding = binding.model_copy(update={"revision_id": str(uuid4())})
    elif failure == "wrong_core":
        binding = binding.model_copy(update={"client_id": "different-client"})
    elif failure == "wrong_db_digest":
        binding = binding.model_copy(update={"config_digest": "f" * 64})
    elif failure == "wrong_public_digest":
        binding = binding.model_copy(update={"configuration_digest": "f" * 64})
    elif failure == "revoked":
        m.rp.authority.unlink()
    elif failure == "expired":
        expired = m.service._reviewed(binding).expires_at + timedelta(seconds=1)
        d.services.casdoor_deployment_policy._now = lambda: expired
    else:
        m.rp.manifest.pop("rp_logout")
        m.rp.resign()
    with pytest.raises((RPLogoutUnavailable, CasdoorConfigurationError, DeploymentEvidenceError)):
        m.service._reviewed(binding)
    assert m.service.observation(binding) is None
    with d.f.service._session_factory() as session:
        assert session.get(CasdoorNamespaceExtend, m.rp.binding.namespace_id).fence_epoch == epoch
