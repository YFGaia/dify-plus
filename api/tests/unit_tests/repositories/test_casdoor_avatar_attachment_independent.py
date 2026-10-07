"""Independent committed-factory audit-trigger rollback and fresh readback proof."""

import re

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from test_casdoor_avatar_attachment_extend import attach, avatar_fixture, owner, snapshot, storage_fixture  # noqa: F401
from test_casdoor_avatar_attachment_extend import attachment as configured_attachment  # noqa: F401
from test_casdoor_profile_repository_extend import NOW

from models.account import Account
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict


def test_audit_trigger_mutation_rolls_back_claim_then_fresh_attachment_reconciles_read_only(request):
    s = request.getfixturevalue("configured_attachment")
    with s.maker() as session, session.begin():
        session.execute(
            sa.text(
                "CREATE TRIGGER mutate_avatar_attach_audit AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_attach' BEGIN "
                "UPDATE casdoor_audit_extend SET summary_json=printf('%020000d',1), "
                "correlation_id='0000000000000000000000000000000000000000000000000000000000000000' "
                "WHERE id=NEW.id; END"
            )
        )

    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict), s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(name="earlier caller write"))
        session.flush()
        owner(s, session).recheck_and_attach(s.claimed.attempt, s.normalized, now=NOW)

    assert snapshot(s) == before
    with s.maker() as reader:
        claim = reader.get(Intent, str(s.intent_id))
        account = reader.get(Account, s.account.id)
        assert claim.operation_state == "in_flight"
        assert claim.termination_state == "unconfirmed"
        assert account.name != "earlier caller write"
        assert reader.get(UploadFile, s.claimed.reservation.file_id) is None
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_attach")) == 0

    with s.maker() as session, session.begin():
        session.execute(sa.text("DROP TRIGGER mutate_avatar_attach_audit"))

    attached = attach(s)
    assert attached.code == "attached"
    with s.maker() as reader:
        assert reader.get(Account, s.account.id).avatar == str(attached.result_file_id)
        assert reader.get(Intent, str(s.intent_id)).operation_state == "applied"
        assert reader.get(UploadFile, str(attached.result_file_id)) is not None
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_attach")) == 1

    dml = []

    def count_dml(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.IGNORECASE):
            dml.append(statement)

    engine = s.maker.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_dml)
    try:
        with s.maker() as reader:
            reader.begin()
            outcome = owner(s, reader).reconcile_attachment(s.claimed.attempt, now=NOW)
            reader.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", count_dml)

    assert outcome.code == "applied"
    assert outcome.result_file_id == attached.result_file_id
    assert not dml
    assert not s.io_calls
