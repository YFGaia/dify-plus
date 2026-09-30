"""Fork statistics are initialized by every app-creation signal in its caller session."""

from types import SimpleNamespace

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from events.app_event import app_was_created
from events.event_handlers import create_app_statistics_when_app_created_extend as module
from models.model_extend import AppStatisticsExtend


def test_creation_signal_registers_statistics_handler() -> None:
    assert app_was_created.has_receivers_for(SimpleNamespace(id="app-a"))
    assert module.handle in app_was_created.receivers_for(SimpleNamespace(id="app-a"))


def test_creation_initializes_once_without_committing(sqlite_session: Session) -> None:
    commits: list[str] = []

    def after_commit(_session: Session) -> None:
        commits.append("commit")

    event.listen(sqlite_session, "after_commit", after_commit)
    try:
        app = SimpleNamespace(id="app-a")
        module.handle(app, session=sqlite_session)
        module.handle(app, session=sqlite_session)
        rows = sqlite_session.scalars(select(AppStatisticsExtend).where(AppStatisticsExtend.app_id == app.id)).all()
        assert len(rows) == 1
        assert rows[0].number == 0
        assert rows[0].id
        assert commits == []
        sqlite_session.rollback()
        assert sqlite_session.scalar(select(AppStatisticsExtend.id).where(AppStatisticsExtend.app_id == app.id)) is None
    finally:
        event.remove(sqlite_session, "after_commit", after_commit)


def test_existing_usage_is_never_reset(sqlite_session: Session) -> None:
    existing = AppStatisticsExtend(id="existing", app_id="app-a", number=9)
    sqlite_session.add(existing)
    sqlite_session.flush()
    module.handle(SimpleNamespace(id="app-a"), session=sqlite_session)
    assert sqlite_session.scalars(select(AppStatisticsExtend)).all() == [existing]
    assert existing.number == 9
