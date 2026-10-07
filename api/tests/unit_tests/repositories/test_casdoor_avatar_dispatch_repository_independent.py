"""Caller-root rollback keeps cursor faults from publishing partial navigation."""

import pytest
import sqlalchemy as sa
from models.account import Account
from models.casdoor_extend import CasdoorAvatarDispatchCursorExtend as Cursor
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict
from test_casdoor_avatar_dispatch_repository_extend import cursor, page, snapshot
from test_casdoor_avatar_attempt_extend import avatar_fixture as original_avatar_fixture
from test_casdoor_avatar_attempt_extend import (
    storage_fixture as original_storage_fixture,
)
from test_casdoor_avatar_attempt_extend import worker as original_worker
from test_casdoor_profile_repository_extend import NOW

avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture
worker = original_worker


def test_corrupt_cursor_rolls_back_callers_prior_write_and_recovers_only_after_rollback(
    worker,
):
    s = worker
    Cursor.__table__.create(s.session.get_bind())

    # Start from a real committed producer and durable cursor owned by the repository.
    assert page(s).intent_ids == (s.intent_id,)
    before = snapshot(s)
    before_cursor = cursor(s)

    # SQLite INTEGER affinity keeps this committed REAL; the owner cannot repair it.
    with s.session.begin():
        s.session.execute(
            sa.text(
                "UPDATE casdoor_avatar_dispatch_cursor_extend "
                "SET version = 1.5 WHERE slot = 1"
            )
        )
    fault_cursor = cursor(s)
    assert fault_cursor["version"] == 1.5
    assert {key: value for key, value in fault_cursor.items() if key != "version"} == {
        key: value for key, value in before_cursor.items() if key != "version"
    }
    with s.session.begin():
        s.session.execute(
            sa.update(Account)
            .where(Account.id == s.account.id)
            .values(name="caller-write")
        )
        with pytest.raises(CasdoorAvatarConflict):
            s.avatar_owner._scan_initial_dispatch_page(now=NOW)
        # Failure stays in this root; the caller owns rollback.
        s.session.rollback()

    assert cursor(s) == fault_cursor
    assert snapshot(s) == before
    # Repair is explicit and separate. A valid root can then recover the producer.
    with s.session.begin():
        s.session.execute(
            sa.text(
                "UPDATE casdoor_avatar_dispatch_cursor_extend "
                "SET version = 1 WHERE slot = 1"
            )
        )
    assert page(s).intent_ids == ()
    assert page(s).intent_ids == (s.intent_id,)
    assert snapshot(s) == before
