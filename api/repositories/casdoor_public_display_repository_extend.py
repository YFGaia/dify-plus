"""Minimal public active metadata; no draft, policy or credential projection.

This is presentation metadata only. Ordinary admission and activation retain their
full revision/digest/deployment-policy owners and do not consume this projection.
"""

import sqlalchemy as sa
from core.casdoor.configuration import ButtonText
from core.casdoor.errors import CasdoorErrorCode
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorNamespaceLifecycle
from pydantic import TypeAdapter

from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError, _DisplayMetadata


class CasdoorPublicDisplayRepository:
    def __init__(self, session):
        self.session = session

    def display(self):
        with self.session.no_autoflush:
            integration = self.session.execute(
                sa.select(Integration.id, Integration.enabled, Integration.active_revision_id).where(
                    Integration.slot == 1
                )
            ).one_or_none()
            if integration is None or integration.enabled is not True:
                return _DisplayMetadata()
            active = self.session.execute(
                sa.select(Revision.button_text, Namespace.lifecycle)
                .join(Namespace, Namespace.id == Revision.namespace_id)
                .where(
                    Revision.id == integration.active_revision_id,
                    Revision.integration_id == integration.id,
                    Namespace.integration_id == integration.id,
                )
            ).one_or_none()
            if active is None or active.lifecycle != CasdoorNamespaceLifecycle.ACTIVE:
                raise CasdoorConfigurationError(CasdoorErrorCode.CONFIG_CONFLICT, "active_display_unavailable")
            return _DisplayMetadata(
                enabled=True, button_text=TypeAdapter(ButtonText).validate_python(active.button_text)
            )
