"""Independent SQL-only reservation fence boundary cases."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from test_casdoor_avatar_attempt_extend import claim
from test_casdoor_avatar_attempt_extend import worker as original_worker
from test_casdoor_avatar_intent_extend import avatar as original_avatar_fixture
from test_casdoor_profile_repository_extend import storage as original_storage_fixture

from machinery.context import RequestContext
from models.account import Account
from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend as Guard
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.account_repository import SQLAlchemyAccountRepository
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from services.account_errors import AvatarFileNotFoundError
from services.account_profile_service import AccountProfileService
from services.entities.account_entities import AccountProfileChanges

worker = original_worker
avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture


@pytest.fixture
def fenced_worker(worker):
    Guard.__table__.create(worker.session.get_bind(), checkfirst=True)
    Audit.__table__.create(worker.session.get_bind(), checkfirst=True)
    return worker


def _snapshot(session):
    return {
        model.__tablename__: [
            tuple(row)
            for row in session.execute(sa.select(*model.__table__.columns).order_by(list(model.__table__.columns)[0]))
        ]
        for model in (Account, Guard, Audit, Intent)
    }


def _profile_service(s):
    return AccountProfileService(accounts=SQLAlchemyAccountRepository(sessionmaker(s.session.get_bind())))


def _context(account_id):
    return RequestContext("independent-fence", None, account_id, str(uuid4()))


def test_multibyte_oversize_summary_is_rejected_before_full_row_hydration(fenced_worker):
    s = fenced_worker
    result = claim(s)
    oversized = "é" * 2049  # 2049 code points but 4098 UTF-8 bytes.
    assert len(oversized) <= 4096 and len(oversized.encode("utf-8")) > 4096
    with Session(s.session.get_bind()) as session, session.begin():
        session.execute(
            sa.update(Audit).where(Audit.action == "avatar_reservation_fence").values(summary_json=oversized)
        )

    full_projection = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "casdoor_audit_extend" in statement:
            names = {column[0] for column in cursor.description}
            if "summary_json" in names:
                full_projection.append(True)

    engine = s.session.get_bind()
    sa.event.listen(engine, "after_cursor_execute", observe)
    try:
        with Session(engine) as session, session.begin(), pytest.raises(ValueError):
            CasdoorAuditRepository(session)._read_avatar_reservation_fence(result.reservation.file_id)
        assert not full_projection, "Oversized UTF-8 audit text reached full scalar-row projection"
    finally:
        sa.event.remove(engine, "after_cursor_execute", observe)


def test_duplicate_json_member_cannot_shadow_a_real_reservation(fenced_worker):
    s = fenced_worker
    result = claim(s)
    with Session(s.session.get_bind()) as session, session.begin():
        original = session.scalar(sa.select(Audit.summary_json).where(Audit.action == "avatar_reservation_fence"))
        # A normal json.loads keeps only the last duplicate. The closed parser must refuse it.
        damaged = '{"schema_version":999,' + original[1:]
        json.loads(damaged)
        session.execute(
            sa.update(Audit).where(Audit.action == "avatar_reservation_fence").values(summary_json=damaged)
        )

    before = _snapshot(s.session)
    with pytest.raises(AvatarFileNotFoundError):
        _profile_service(s).update(
            _context(s.account.id),
            AccountProfileChanges(name="Must Roll Back", avatar=result.reservation.file_id),
        )
    with Session(s.session.get_bind()) as reader:
        assert _snapshot(reader) == before
