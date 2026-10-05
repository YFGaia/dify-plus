"""Exact historical revision lookup for the optional logout policy owner.

An old valid source may exit after integration disable or a newer configuration.
It must never borrow that newer revision's endpoint/profile. This read-only owner
reconstructs the complete persisted scope, closes SQL, then reads signed evidence.
"""

from uuid import UUID

from core.casdoor.deployment_evidence import DeploymentBinding
from models.casdoor_extend import CasdoorIntegrationExtend, CasdoorNamespaceExtend, CasdoorNamespaceLifecycle

from services.casdoor_rp_logout_service_extend import RPLogoutUnavailable


class CasdoorRPLogoutPolicyReader:
    def __init__(self, *, session_factory, configuration_service, deployment_policy_service, settings):
        self._sessions = session_factory
        self._configuration = configuration_service
        self._deployment = deployment_policy_service
        self._settings = settings

    def __call__(self, binding):
        if (
            type(binding) is not DeploymentBinding
            or self._settings.RBAC_ENABLED is not False
            or self._configuration._rbac_enabled is not False
        ):
            raise RPLogoutUnavailable()
        with self._sessions() as session:
            owner = self._configuration._repository(session)
            namespace = session.get(CasdoorNamespaceExtend, binding.namespace_id)
            if namespace is None:
                raise RPLogoutUnavailable()
            integration = session.get(CasdoorIntegrationExtend, namespace.integration_id)
            if integration is None or integration.slot != 1:
                raise RPLogoutUnavailable()
            # Disable fences login/sync, but must not revoke a still-valid historical
            # logout snapshot. Only the disabled integration gets this exit-only
            # exception; other fencing and archived namespaces stay unavailable.
            if namespace.lifecycle is not CasdoorNamespaceLifecycle.ACTIVE and not (
                namespace.lifecycle is CasdoorNamespaceLifecycle.FENCING and integration.enabled is False
            ):
                raise RPLogoutUnavailable()
            revision = owner._revision(integration.id, binding.revision_id)
            configuration = owner._configuration(revision)
            if (
                revision.namespace_id != binding.namespace_id
                or revision.config_digest != binding.config_digest
                or configuration.config_digest() != binding.configuration_digest
                or not configuration.rp_logout
                or (namespace.expected_issuer, namespace.organization, namespace.application, namespace.client_id)
                != (binding.expected_issuer, binding.organization, binding.application, binding.client_id)
            ):
                raise RPLogoutUnavailable()
            namespace_id, revision_id, digest = UUID(namespace.id), UUID(revision.id), revision.config_digest
        policy = self._deployment.resolve(
            configuration=configuration,
            namespace_id=namespace_id,
            revision_id=revision_id,
            config_digest=digest,
            rbac_mode="off",
        )
        if policy.binding != binding:
            raise RPLogoutUnavailable()
        return policy
