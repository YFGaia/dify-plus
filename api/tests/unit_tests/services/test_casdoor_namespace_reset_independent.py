"""Independent actual-route boundary check for an integration pointer outside reset scope."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorNamespaceLifecycle,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from test_casdoor_namespace_reset_flow_extend import MODELS, RESET, rows, send

pytest_plugins = ("test_casdoor_namespace_reset_flow_extend",)


def test_cross_namespace_pointer_fails_closed_before_review(resettable):
    d = resettable
    with d.f.service._session_factory() as session, session.begin():
        integration = session.scalar(sa.select(Integration))
        original = session.get(Revision, integration.draft_revision_id)
        active_pointer_before = integration.active_revision_id
        old_namespace = session.get(Namespace, d.reset_input["namespace_id"])
        namespace_id = str(uuid4())
        revision_id = str(uuid4())
        foreign_namespace = Namespace(
            id=namespace_id,
            integration_id=integration.id,
            expected_issuer=old_namespace.expected_issuer,
            organization=old_namespace.organization,
            application=old_namespace.application,
            client_id=old_namespace.client_id,
            core_fingerprint=old_namespace.core_fingerprint,
            lifecycle=CasdoorNamespaceLifecycle.ARCHIVED,
            fence_epoch=old_namespace.fence_epoch + 1,
            archived_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(foreign_namespace)
        session.flush()
        revision_values = {
            column.name: getattr(original, column.name)
            for column in Revision.__table__.columns
            if column.name not in {"id", "created_at", "namespace_id", "revision_number"}
        }
        foreign_revision = Revision(
            id=revision_id,
            namespace_id=namespace_id,
            revision_number=original.revision_number + 1,
            **revision_values,
        )
        session.add(foreign_revision)
        session.flush()
        integration.draft_revision_id = revision_id
        session.flush()

    before = rows(d, (*MODELS, Intent))
    assert not d.review_records
    assert not d.f.control.tokens
    response = send(d, RESET + "/review", method="POST", json=d.reset_input)
    assert response.status_code == 400, response.json
    assert rows(d, (*MODELS, Intent)) == before
    assert not d.review_records
    assert not d.f.control.tokens
    with d.f.service._session_factory() as session:
        integration = session.scalar(sa.select(Integration))
        assert integration.enabled is False
        assert integration.active_revision_id == active_pointer_before
        assert integration.draft_revision_id == revision_id
