"""Tenant/app/conversation scoping for fork context markers."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from werkzeug.exceptions import NotFound

from models.enums import ConversationFromSource
from models.model import App, AppMode, Conversation, IconType
from models.model_extend import MessageContextExtend
from services import recommended_app_service_extend as module


@pytest.fixture
def context_db(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setattr(module, "db", SimpleNamespace(session=sqlite_session))
    for app_id, tenant_id in [("app-a", "tenant-a"), ("app-b", "tenant-b")]:
        sqlite_session.add(
            App(
                id=app_id,
                tenant_id=tenant_id,
                name=app_id,
                description="",
                mode=AppMode.CHAT,
                icon_type=IconType.EMOJI,
                icon="chat",
                icon_background="#ffffff",
                enable_site=True,
                enable_api=True,
            )
        )
    for conversation_id, app_id, deleted in [
        ("c-a", "app-a", False),
        ("c-other", "app-a", False),
        ("c-b", "app-b", False),
        ("c-deleted", "app-a", True),
    ]:
        sqlite_session.add(
            Conversation(
                id=conversation_id,
                app_id=app_id,
                mode=AppMode.CHAT,
                name="Context",
                inputs={},
                from_source=ConversationFromSource.CONSOLE,
                is_deleted=deleted,
            )
        )
        for index, message_id in enumerate(["old", "new"]):
            sqlite_session.add(
                MessageContextExtend(
                    id=f"{conversation_id}-{message_id}",
                    conversation_id=conversation_id,
                    message_id=message_id,
                    created_at=datetime(2026, 1, 1) + timedelta(seconds=index),
                )
            )
    sqlite_session.commit()
    return sqlite_session


@pytest.mark.parametrize("conversation_id", ["missing", "c-b", "c-deleted"])
def test_invalid_owner_returns_not_found_without_context_query(context_db: Session, conversation_id: str) -> None:
    statements: list[str] = []
    engine = context_db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(NotFound):
            module.RecommendedAppService.message_context_app(tenant_id="tenant-a", conversation_id=conversation_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements
    assert not any("message_context_extend" in statement for statement in statements)


def test_deleted_app_returns_not_found(context_db: Session) -> None:
    context_db.query(App).filter(App.id == "app-a").delete(synchronize_session=False)
    context_db.commit()
    with pytest.raises(NotFound):
        module.RecommendedAppService.message_context_app(tenant_id="tenant-a", conversation_id="c-a")


def test_owner_resolution_and_newest_first_markers(context_db: Session) -> None:
    app = module.RecommendedAppService.message_context_app(tenant_id="tenant-a", conversation_id="c-a")
    assert app is context_db.get(App, "app-a")
    assert module.RecommendedAppService.message_context(tenant_id="tenant-a", app_id=app.id, conversation_id="c-a") == [
        "new",
        "old",
    ]


def test_delete_only_selected_conversation_and_marker(context_db: Session) -> None:
    assert (
        module.RecommendedAppService.delete_message_context(
            tenant_id="tenant-a", app_id="app-a", conversation_id="c-a", message_id="new"
        )
        == "ok"
    )
    remaining = {(marker.conversation_id, marker.message_id) for marker in context_db.query(MessageContextExtend).all()}
    assert remaining == {
        (conversation_id, message_id)
        for conversation_id in ["c-a", "c-other", "c-b", "c-deleted"]
        for message_id in ["old", "new"]
        if (conversation_id, message_id) != ("c-a", "new")
    }


@pytest.mark.parametrize(
    ("tenant_id", "app_id", "conversation_id"),
    [("tenant-b", "app-a", "c-a"), ("tenant-a", "app-b", "c-a"), ("tenant-a", "app-a", "c-deleted")],
)
def test_context_operations_retain_complete_owner_scope(
    context_db: Session, tenant_id: str, app_id: str, conversation_id: str
) -> None:
    scope = {"tenant_id": tenant_id, "app_id": app_id, "conversation_id": conversation_id}
    assert module.RecommendedAppService.message_context(**scope) == []
    assert module.RecommendedAppService.delete_message_context(**scope, message_id="new") == "ok"
    assert context_db.query(MessageContextExtend).count() == 8


def test_delete_commits_only_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.query.return_value.filter.return_value.delete.side_effect = RuntimeError("database failure")
    monkeypatch.setattr(module, "db", SimpleNamespace(session=session))
    with pytest.raises(RuntimeError, match="database failure"):
        module.RecommendedAppService.delete_message_context(
            tenant_id="t", app_id="a", conversation_id="c", message_id="m"
        )
    session.commit.assert_not_called()
