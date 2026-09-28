import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from models.enums import CustomizeTokenStrategy
from models.model import App, AppMode, Site
from models.model_extend import AppExtend
from repositories.app_site_command_repository import AppSiteCommandRepository
from services.app_site_service import AppSiteAppNotFoundError, AppSiteChanges, AppSiteNotFoundError
from services.app_site_service_extend import AppSiteChangesExtend
from services.webapp_auth_service_extend import WebAppAuthExtendService

_APP_ID = "11111111-1111-1111-1111-111111111111"
_WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"
_OTHER_WORKSPACE_ID = "33333333-3333-3333-3333-333333333333"
_ACTOR_ID = "44444444-4444-4444-4444-444444444444"


def _persist_app(session: Session, *, with_site: bool = True) -> None:
    app = App(
        id=_APP_ID,
        tenant_id=_WORKSPACE_ID,
        name="Site App",
        description="",
        mode=AppMode.CHAT,
        icon_type=None,
        icon=None,
        icon_background=None,
        enable_site=True,
        enable_api=True,
    )
    session.add(app)
    if with_site:
        session.add(
            Site(
                app_id=_APP_ID,
                title="Original",
                description="Original description",
                default_language="en-US",
                input_placeholder="Original placeholder",
                customize_token_strategy=CustomizeTokenStrategy.NOT_ALLOW,
                prompt_public=False,
                show_workflow_steps=True,
                use_icon_as_answer_icon=False,
                code="old-code",
            )
        )
    session.commit()


def _repository(session_factory: sessionmaker[Session]) -> AppSiteCommandRepository:
    return AppSiteCommandRepository(session_factory=session_factory)


def test_update_preserves_none_and_writes_false_and_empty_values(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    _persist_app(sqlite_session)

    result = _repository(sqlite_session_factory).update_site(
        workspace_id=_WORKSPACE_ID,
        app_id=_APP_ID,
        actor_id=_ACTOR_ID,
        changes=AppSiteChanges(
            title=None,
            input_placeholder="",
            customize_token_strategy="allow",
            show_workflow_steps=False,
        ),
    )

    assert result.title == "Original"
    assert result.input_placeholder == ""
    assert result.customize_token_strategy == "allow"
    assert result.show_workflow_steps is False
    with sqlite_session_factory() as session:
        site = session.scalar(select(Site).where(Site.app_id == _APP_ID))
        assert site is not None
        assert site.title == "Original"
        assert site.input_placeholder == ""
        assert site.customize_token_strategy == CustomizeTokenStrategy.ALLOW
        assert site.show_workflow_steps is False
        assert site.updated_by == _ACTOR_ID


def test_update_scopes_app_to_workspace_and_distinguishes_missing_site(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    _persist_app(sqlite_session)
    repository = _repository(sqlite_session_factory)

    with pytest.raises(AppSiteAppNotFoundError):
        repository.update_site(
            workspace_id=_OTHER_WORKSPACE_ID,
            app_id=_APP_ID,
            actor_id=_ACTOR_ID,
            changes=AppSiteChanges(title="Leaked"),
        )

    with sqlite_session_factory.begin() as session:
        session.execute(delete(Site).where(Site.app_id == _APP_ID))

    with pytest.raises(AppSiteNotFoundError):
        repository.update_site(
            workspace_id=_WORKSPACE_ID,
            app_id=_APP_ID,
            actor_id=_ACTOR_ID,
            changes=AppSiteChanges(title="Missing"),
        )


def test_update_rolls_back_when_a_site_field_rejects_the_value(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    _persist_app(sqlite_session)

    with pytest.raises(ValueError, match="cannot exceed 512"):
        _repository(sqlite_session_factory).update_site(
            workspace_id=_WORKSPACE_ID,
            app_id=_APP_ID,
            actor_id=_ACTOR_ID,
            changes=AppSiteChanges(title="Changed", custom_disclaimer="x" * 513),
        )

    with sqlite_session_factory() as session:
        site = session.scalar(select(Site).where(Site.app_id == _APP_ID))
        assert site is not None
        assert site.title == "Original"
        assert site.updated_by is None


def test_reset_access_token_uses_the_owned_transaction(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_app(sqlite_session)
    observed_session: Session | None = None

    def generate_code(length: int, *, session: Session) -> str:
        nonlocal observed_session
        observed_session = session
        assert length == 16
        assert session.in_transaction()
        return "new-code"

    monkeypatch.setattr(Site, "generate_code", generate_code)

    result = _repository(sqlite_session_factory).reset_access_token(
        workspace_id=_WORKSPACE_ID,
        app_id=_APP_ID,
        actor_id=_ACTOR_ID,
    )

    assert observed_session is not None
    assert result.code == "new-code"
    with sqlite_session_factory() as session:
        site = session.scalar(select(Site).where(Site.app_id == _APP_ID))
        assert site is not None
        assert site.code == "new-code"
        assert site.updated_by == _ACTOR_ID


@pytest.mark.parametrize("enabled", [True, False])
def test_site_and_auth_extend_persist_atomically_and_invalidate_after_commit(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    _persist_app(sqlite_session)
    calls: list[tuple[str, bool]] = []
    original_setter = WebAppAuthExtendService.set_webapp_auth_enabled
    write_sessions: list[Session] = []

    def write_extend(app_id: str, value: bool, *, session: Session) -> None:
        calls.append(("write", session.in_transaction()))
        write_sessions.append(session)
        original_setter(app_id, value, session=session)

    def invalidate(app_id: str) -> None:
        calls.append(("invalidate", app_id == _APP_ID))
        assert not write_sessions[0].in_transaction()
        # The repository must call this only after the owned transaction commits.
        with sqlite_session_factory() as verify_session:
            persisted = verify_session.scalar(select(AppExtend).where(AppExtend.app_id == app_id))
            assert persisted is not None
            assert persisted.webapp_auth_enabled is enabled

    monkeypatch.setattr(WebAppAuthExtendService, "set_webapp_auth_enabled", write_extend)
    monkeypatch.setattr(WebAppAuthExtendService, "invalidate_after_commit", invalidate)

    _repository(sqlite_session_factory).update_site(
        workspace_id=_WORKSPACE_ID,
        app_id=_APP_ID,
        actor_id=_ACTOR_ID,
        changes=AppSiteChangesExtend(title="Changed", webapp_auth_enabled_extend=enabled),
    )

    assert calls == [("write", True), ("invalidate", True)]
    with sqlite_session_factory() as session:
        site = session.scalar(select(Site).where(Site.app_id == _APP_ID))
        extend = session.scalar(select(AppExtend).where(AppExtend.app_id == _APP_ID))
        assert site is not None
        assert site.title == "Changed"
        assert extend is not None
        assert extend.webapp_auth_enabled is enabled


def test_auth_update_failure_rolls_back_site_and_does_not_invalidate(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_app(sqlite_session)
    invalidated: list[str] = []

    def fail_write(app_id: str, value: bool, *, session: Session) -> None:
        session.add(AppExtend(app_id=app_id, webapp_auth_enabled=value))
        raise RuntimeError("extend write failed")

    monkeypatch.setattr(WebAppAuthExtendService, "set_webapp_auth_enabled", fail_write)
    monkeypatch.setattr(WebAppAuthExtendService, "invalidate_after_commit", invalidated.append)

    with pytest.raises(RuntimeError, match="extend write failed"):
        _repository(sqlite_session_factory).update_site(
            workspace_id=_WORKSPACE_ID,
            app_id=_APP_ID,
            actor_id=_ACTOR_ID,
            changes=AppSiteChangesExtend(title="Must roll back", webapp_auth_enabled_extend=False),
        )

    assert invalidated == []
    with sqlite_session_factory() as session:
        site = session.scalar(select(Site).where(Site.app_id == _APP_ID))
        assert site is not None
        assert site.title == "Original"
        assert site.updated_by is None
        assert session.scalar(select(AppExtend).where(AppExtend.app_id == _APP_ID)) is None


def test_null_extension_flag_leaves_auth_row_and_cache_untouched(
    sqlite_session: Session,
    sqlite_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_app(sqlite_session)
    sqlite_session.add(AppExtend(id="55555555-5555-5555-5555-555555555555", app_id=_APP_ID, webapp_auth_enabled=None))
    sqlite_session.commit()
    invalidated: list[str] = []
    monkeypatch.setattr(WebAppAuthExtendService, "invalidate_after_commit", invalidated.append)

    _repository(sqlite_session_factory).update_site(
        workspace_id=_WORKSPACE_ID,
        app_id=_APP_ID,
        actor_id=_ACTOR_ID,
        changes=AppSiteChangesExtend(title="Site only", webapp_auth_enabled_extend=None),
    )

    assert invalidated == []
    with sqlite_session_factory() as session:
        extend = session.scalar(select(AppExtend).where(AppExtend.app_id == _APP_ID))
        assert extend is not None
        assert extend.webapp_auth_enabled is None


@pytest.mark.parametrize("existing", [False, True])
def test_commit_failure_rolls_back_real_site_and_extension(
    sqlite_session, sqlite_session_factory, monkeypatch, existing
):
    from sqlalchemy import event

    _persist_app(sqlite_session)
    if existing:
        sqlite_session.add(
            AppExtend(id="55555555-5555-5555-5555-555555555555", app_id=_APP_ID, webapp_auth_enabled=True)
        )
        sqlite_session.commit()
    invalidated = []
    monkeypatch.setattr(WebAppAuthExtendService, "invalidate_after_commit", invalidated.append)

    def fail_commit(_session):
        raise RuntimeError("commit unavailable")

    event.listen(sqlite_session_factory.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit unavailable"):
            _repository(sqlite_session_factory).update_site(
                workspace_id=_WORKSPACE_ID,
                app_id=_APP_ID,
                actor_id=_ACTOR_ID,
                changes=AppSiteChangesExtend(title="rollback", webapp_auth_enabled_extend=False),
            )
    finally:
        event.remove(sqlite_session_factory.class_, "before_commit", fail_commit)
    assert not invalidated
    with sqlite_session_factory() as session:
        assert session.scalar(select(Site).where(Site.app_id == _APP_ID)).title == "Original"
        row = session.scalar(select(AppExtend).where(AppExtend.app_id == _APP_ID))
        assert (row is not None and row.webapp_auth_enabled is True) if existing else row is None


@pytest.mark.parametrize("enabled", [False, True])
def test_failed_cache_invalidation_observes_committed_switch(
    sqlite_session, sqlite_session_factory, monkeypatch, enabled
):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sqlalchemy.orm import scoped_session

    from services import webapp_auth_service_extend as service_module

    _persist_app(sqlite_session)
    sqlite_session.add(
        AppExtend(id="55555555-5555-5555-5555-555555555555", app_id=_APP_ID, webapp_auth_enabled=not enabled)
    )
    sqlite_session.commit()
    sessions = scoped_session(sqlite_session_factory)
    monkeypatch.setattr(service_module, "db", SimpleNamespace(session=sessions))
    cache = MagicMock()
    cache.get.return_value = b"0" if enabled else b"1"
    cache.delete.side_effect = ConnectionError("redis down")
    cache.set.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(service_module, "redis_client", cache)
    try:
        # Prime the Session identity map with the old public row as well.
        assert WebAppAuthExtendService.is_webapp_auth_enabled(_APP_ID) is not enabled
        _repository(sqlite_session_factory).update_site(
            workspace_id=_WORKSPACE_ID,
            app_id=_APP_ID,
            actor_id=_ACTOR_ID,
            changes=AppSiteChangesExtend(webapp_auth_enabled_extend=enabled),
        )
        assert WebAppAuthExtendService.is_webapp_auth_enabled(_APP_ID) is enabled
        cache.delete.assert_called_once_with(f"webapp_auth_enabled_extend:{_APP_ID}")
    finally:
        sessions.remove()
