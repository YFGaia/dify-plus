"""Independently pin the final strict-replay wrapper reread boundary."""

import json
from uuid import uuid4

import sqlalchemy as sa
from test_casdoor_avatar_attachment_extend import avatar_fixture, storage_fixture  # noqa: F401
from test_casdoor_avatar_io_authority_extend import (
    attach,
    configured_attachment,  # noqa: F401, F811
    owner,
    readonly,
    snapshot,
)
from test_casdoor_profile_repository_extend import NOW

from models.account import Account
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_profile_repository_extend import _dump


def test_final_wrapper_reread_rejects_late_intent_drift_and_rollback_restores_replay(
    configured_attachment,  # noqa: F811
):
    s = configured_attachment
    attached = attach(s)
    assert attached.code == "attached"
    with s.maker() as reader:
        data = json.loads(reader.scalar(sa.select(Intent.desired_json).where(Intent.id == str(s.intent_id))))
    data["correlation_id"] = str(uuid4())
    mutation = (
        sa.update(Intent)
        .where(Intent.id == str(s.intent_id))
        .values(attempt_count=Intent.attempt_count + 1, desired_json=_dump(data))
    )
    intent_reads = []
    repository_dml = []
    callback_dml = []
    callback_active = False
    fired = []

    def observe(connection, cursor, statement, parameters, context, many):
        nonlocal callback_active
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            (callback_dml if callback_active else repository_dml).append(statement)
        if statement.startswith("SELECT casdoor_sync_intent_extend."):
            intent_reads.append(statement)
            if len(intent_reads) == 4:
                fired.append(True)
                callback_active = True
                try:
                    connection.execute(mutation)
                finally:
                    callback_active = False

    before = snapshot(s)
    engine = s.maker.kw["bind"]
    with s.maker() as session:
        transaction = session.begin()
        session.execute(sa.update(Account).values(name="earlier caller work"))
        session.flush()
        sa.event.listen(engine, "before_cursor_execute", observe)
        try:
            outcome = owner(s, session).reconcile_intent_attachment(s.intent_id, now=NOW)
        finally:
            sa.event.remove(engine, "before_cursor_execute", observe)
        assert outcome.code == "unknown" and outcome.result_file_id is None
        assert session.get_transaction() is transaction and transaction.is_active
        session.rollback()

    assert fired and len(intent_reads) == 4
    assert not repository_dml and len(callback_dml) == 1
    assert snapshot(s) == before and not s.io_calls
    with readonly(s) as repository:
        replay = repository.reconcile_intent_attachment(s.intent_id, now=NOW)
        assert replay.code == "applied" and replay.result_file_id == attached.result_file_id
    assert snapshot(s) == before and not s.io_calls
