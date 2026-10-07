"""Real public projection bypasses malformed draft/proof and unrelated flushes."""

from contextlib import nullcontext
from uuid import UUID

import sqlalchemy as sa
from models.account import Tenant
from models.casdoor_extend import CasdoorConfigRevisionExtend, CasdoorIntegrationExtend
from test_casdoor_configuration_service_extend import actor as actor
from test_casdoor_configuration_service_extend import database as database
from test_casdoor_configuration_service_extend import save, service


def test_disabled_display_ignores_stale_draft_and_does_not_resolve_policy(database, actor, monkeypatch):
    factory, workspace_id = database
    owner = service(factory)
    save(owner, actor, workspace_id)
    with factory.begin() as session:
        session.scalar(sa.select(CasdoorIntegrationExtend)).draft_revision_id = str(UUID(int=98765))
    monkeypatch.setattr(
        owner, "_repository", lambda _: (_ for _ in ()).throw(AssertionError("full repository forbidden"))
    )
    assert owner.display().enabled is False


def test_active_public_metadata_ignores_malformed_draft_and_selects_no_credentials(database, actor):
    factory, workspace_id = database
    owner = service(factory)
    first = save(owner, actor, workspace_id)
    second = save(owner, actor, workspace_id, etag=first.etag)
    with factory.begin() as session:
        integration = session.scalar(sa.select(CasdoorIntegrationExtend))
        integration.enabled, integration.active_revision_id = True, str(first.draft_revision_id)
        session.execute(
            sa.update(CasdoorConfigRevisionExtend)
            .where(CasdoorConfigRevisionExtend.id == str(second.draft_revision_id))
            .values(policy_json="malformed draft")
        )
    statements = []

    @sa.event.listens_for(factory.kw["bind"], "before_cursor_execute")
    def query(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    try:
        result = owner.display()
    finally:
        sa.event.remove(factory.kw["bind"], "before_cursor_execute", query)
    assert result.enabled is True and result.button_text == "Casdoor"
    assert len(statements) == 2
    assert not any(
        value in statement
        for statement in statements
        for value in ("encrypted_secret", "draft_revision_id", "policy_json", "config_digest", "certificates_json")
    )


def test_public_projection_does_not_flush_unrelated_pending_tenant(database, actor):
    factory, workspace_id = database
    owner = service(factory)
    save(owner, actor, workspace_id)
    with factory() as session:
        pending = Tenant(name=None)
        session.add(pending)
        owner._session_factory = lambda: nullcontext(session)
        assert owner.display().enabled is False
        assert pending in session.new
        with session.no_autoflush:
            assert session.scalar(sa.select(sa.func.count()).select_from(Tenant)) == 1
