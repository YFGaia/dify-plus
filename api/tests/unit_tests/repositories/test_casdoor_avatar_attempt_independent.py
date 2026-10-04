"""Independent actual-session-factory closure for DB-only avatar attempts."""

import pytest
import sqlalchemy as sa
from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from sqlalchemy.orm import Session
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_avatar_intent_extend import pending as original_pending
from test_casdoor_profile_repository_extend import NOW
from test_casdoor_profile_repository_extend import storage as original_storage

avatar_fixture = original_avatar
storage_fixture = original_storage


@pytest.fixture
def worker(avatar_fixture, monkeypatch):
    state = avatar_fixture
    with state.session.begin():
        join = TenantAccountJoin(
            tenant_id=state.revision.default_workspace_id, account_id=state.account.id
        )
        state.session.add(join)
        state.session.flush()
        state.intent_id = original_pending(state).intent_id
        state.join_id = join.id

    state.io_calls = []

    def denied(*args, **kwargs):
        state.io_calls.append(True)
        raise AssertionError("DB-only avatar attempt reached an I/O or file seam")

    from extensions.ext_storage import storage
    from PIL import Image
    from services.file_service import FileService

    for name in (
        "normalize_avatar_image",
        "store_reserved_avatar",
        "insert_reserved_avatar",
    ):
        monkeypatch.setattr(FileService, name, denied)
    for name in ("encrypt", "decrypt"):
        monkeypatch.setattr(type(state.config_owner.crypto), name, denied)
    for name in ("save", "load_stream", "load_once", "delete", "exists"):
        monkeypatch.setattr(storage, name, denied)
    monkeypatch.setattr(Image, "open", denied)
    yield state
    assert not state.io_calls


def test_factory_claim_replaced_join_finish_audit_rollback_and_fresh_unknown(
    worker, monkeypatch
):
    import core.db.session_factory as factory

    state = worker
    monkeypatch.setattr(factory, "_session_maker", factory._session_maker)
    factory.configure_session_factory(state.session.get_bind())
    maker = factory.get_session_maker()

    def owner(session):
        config = CasdoorConfigurationRepository(
            session, crypto=state.config_owner.crypto, rbac_enabled=False
        )
        return CasdoorAvatarRepository(session, configuration_repository=config)

    with maker() as session, session.begin():
        assert isinstance(session, Session) and type(session) is not Session
        claimed = owner(session).claim_and_reserve(state.intent_id, now=NOW)
        assert claimed.code == "reserved"

    with maker() as session, session.begin():
        session.execute(
            sa.delete(TenantAccountJoin).where(TenantAccountJoin.id == state.join_id)
        )
        session.flush()
        replacement = TenantAccountJoin(
            tenant_id=state.revision.default_workspace_id,
            account_id=state.account.id,
        )
        session.add(replacement)
        session.flush()
        assert replacement.id != state.join_id
        session.execute(
            sa.text(
                "CREATE TRIGGER independent_finish_audit_fault AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_finish' BEGIN SELECT RAISE(ABORT, 'synthetic finish audit failure'); END"
            )
        )

    with maker() as reader:
        original_name = reader.scalar(
            sa.select(Account.name).where(Account.id == state.account.id)
        )
    with pytest.raises(
        CasdoorAvatarConflict, match="^casdoor_avatar_conflict$"
    ), maker() as session, session.begin():
        session.execute(
            sa.update(Account)
            .where(Account.id == state.account.id)
            .values(name="caller flushed write")
        )
        session.flush()
        owner(session).finish_attempt(claimed.attempt, reason="fetch_failed", now=NOW)

    with maker() as session, session.begin():
        session.execute(sa.text("DROP TRIGGER independent_finish_audit_fault"))
    with maker() as reader:
        row = reader.get(Intent, str(state.intent_id))
        account = reader.get(Account, state.account.id)
        audits = list(
            reader.scalars(
                sa.select(Audit)
                .where(Audit.action.in_(("avatar_claim", "avatar_finish")))
                .order_by(Audit.action)
            )
        )
        assert account.name == original_name
        assert row.operation_state == "in_flight"
        assert row.termination_state == "unconfirmed"
        assert (
            row.attempt_count == 1
            and row.retry_at is None
            and row.terminated_at is None
        )
        assert [(entry.action, entry.result_code) for entry in audits] == [
            ("avatar_claim", "reserved")
        ]

    with maker() as session, session.begin():
        result = owner(session).finish_attempt(
            claimed.attempt, reason="fetch_failed", now=NOW
        )
        assert result.code == "unknown"

    with maker() as reader:
        row = reader.get(Intent, str(state.intent_id))
        joins = list(
            reader.scalars(
                sa.select(TenantAccountJoin).where(
                    TenantAccountJoin.tenant_id == state.revision.default_workspace_id,
                    TenantAccountJoin.account_id == state.account.id,
                )
            )
        )
        audits = list(
            reader.scalars(
                sa.select(Audit)
                .where(Audit.action.in_(("avatar_claim", "avatar_finish")))
                .order_by(Audit.action)
            )
        )
        assert row.operation_state == "unknown"
        assert row.termination_state == "manual_recovery"
        assert (
            row.attempt_count == 1
            and row.retry_at is None
            and row.terminated_at is None
        )
        assert row.lease_owner == claimed.attempt.lease_owner
        assert len(joins) == 1 and joins[0].id != state.join_id
        assert [(entry.action, entry.result_code) for entry in audits] == [
            ("avatar_claim", "reserved"),
            ("avatar_finish", "unknown"),
        ]
