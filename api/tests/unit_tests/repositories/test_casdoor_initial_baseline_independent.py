"""Independent SQLite checks for initial-baseline consistency boundaries."""

import hashlib
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import parse_role_baseline_json
from models.casdoor_extend import CasdoorOperationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_attempt_repository_extend import CasdoorRoleAttemptRepository
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict

from tests.unit_tests.repositories import test_casdoor_role_attempt_repository_extend as attempts

pytest_plugins = ("tests.unit_tests.repositories.test_casdoor_role_intent_repository_extend",)


@pytest.fixture
def staged(storage):
    s = storage
    with s.session.begin():
        s.receipt = attempts.staging.enqueue(s)
    s.attempts = CasdoorRoleAttemptRepository(s.session)
    s.reservation_id = uuid4()
    return s


def _different_valid_initial_baseline(s):
    value = json.loads(s.history.baseline_json)
    value["join_role"] = "editor"
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    parse_role_baseline_json(encoded)
    assert encoded != s.history.baseline_json
    return encoded


@pytest.mark.parametrize("reserved", [False, True])
def test_initial_baseline_only_change_blocks_replay_without_dml(staged, reserved):
    s = staged
    if reserved:
        with s.session.begin():
            attempts.reserve(s)
    with s.session.begin():
        before = attempts.snapshot(s)
        s.session.execute(
            sa.update(attempts.staging.History).values(baseline_json=_different_valid_initial_baseline(s))
        )
        changed = attempts.snapshot(s)
        old_history = before[attempts.staging.History.__tablename__][0]._mapping
        new_history = changed[attempts.staging.History.__tablename__][0]._mapping
        assert old_history["desired_roles_json"] == new_history["desired_roles_json"]
        assert old_history["last_applied_roles_json"] == new_history["last_applied_roles_json"]
        assert old_history["last_applied_fingerprint"] == new_history["last_applied_fingerprint"]

    statements = attempts.trace(s)
    for operation in (lambda: attempts.reserve(s), lambda: attempts.staging.enqueue(s)):
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            operation()
        assert not attempts.dml(statements)
    with s.session.begin():
        assert attempts.snapshot(s) == changed


@pytest.mark.parametrize("reserved", [False, True])
def test_legacy_v1_row_bytes_and_state_survive_competing_v2(staged, reserved):
    s = staged
    if reserved:
        with s.session.begin():
            attempts.reserve(s)
    with s.session.begin():
        row = attempts.staging.stored(s)[0]
        payload = json.loads(row.desired_json)
        assert payload["schema_version"] == 2
        payload.pop("initial_baseline_schema_version")
        payload.pop("initial_baseline_digest")
        payload["schema_version"] = 1
        legacy_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        state = CasdoorOperationState.PENDING
        if reserved:
            assert row.attempt_count == 1 and row.attempt_id == str(s.reservation_id)
        else:
            assert row.attempt_count == 0 and row.attempt_id is None
        s.session.execute(
            sa.update(Intent)
            .where(Intent.id == row.id)
            .values(
                desired_json=legacy_json,
                scope_digest=hashlib.sha256(legacy_json.encode("utf-8")).hexdigest(),
                idempotency_key=hashlib.sha256(("casdoor-role-intent-v1:" + legacy_json).encode("utf-8")).hexdigest(),
                operation_state=state,
            )
        )
        before = attempts.snapshot(s)

    statements = attempts.trace(s)
    for operation in (lambda: attempts.staging.enqueue(s), lambda: attempts.reserve(s)):
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            operation()
        assert not attempts.dml(statements)
    with s.session.begin():
        assert attempts.snapshot(s) == before
