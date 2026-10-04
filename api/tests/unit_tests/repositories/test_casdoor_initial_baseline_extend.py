"""F11 local byte/version consistency; fixtures establish no remote authority."""

import hashlib
import json

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import parse_role_baseline_json
from models.casdoor_extend import CasdoorOperationState
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_role_intent_repository_extend import CasdoorRoleIntentConflict

from tests.unit_tests.repositories import test_casdoor_role_attempt_repository_extend as attempts

storage = attempts.storage
staged = attempts.staged


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def changed_baseline(s):
    baseline = json.loads(s.history.baseline_json)
    baseline["roles"] = [
        {
            "role_id": "initial-custom-汉",
            "is_builtin": False,
            "category": "workspace_custom",
            "role_tag": "",
            "permission_keys": ["read"],
        }
    ]
    baseline["join_role"] = "editor"
    result = canonical(baseline)
    parse_role_baseline_json(result)
    return result


@pytest.mark.parametrize("reserved", [False, True])
def test_valid_initial_only_drift_blocks_replay_without_dml(staged, reserved):
    s = staged
    if reserved:
        with s.session.begin():
            attempts.reserve(s)
    with s.session.begin():
        old = s.session.execute(sa.select(*attempts.staging.History.__table__.columns)).one()
        s.session.execute(sa.update(attempts.staging.History).values(baseline_json=changed_baseline(s)))
        after = s.session.execute(sa.select(*attempts.staging.History.__table__.columns)).one()
        assert old.desired_roles_json == after.desired_roles_json
        assert old.last_applied_roles_json == after.last_applied_roles_json
        assert old.last_applied_fingerprint == after.last_applied_fingerprint
        changed = attempts.snapshot(s)
    statements = attempts.trace(s)
    for operation in (lambda: attempts.reserve(s), lambda: attempts.staging.enqueue(s)):
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            operation()
        assert not attempts.dml(statements)
    with s.session.begin():
        assert attempts.snapshot(s) == changed


@pytest.mark.parametrize("reserved", [False, True])
def test_initial_rejection_rolls_back_prior_identity_and_history_write(staged, reserved):
    s = staged
    if reserved:
        with s.session.begin():
            attempts.reserve(s)
    with s.session.begin():
        before = attempts.snapshot(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.execute(sa.update(attempts.staging.Identity).values(profile_sync_json='{"caller":1}'))
        s.session.execute(sa.update(attempts.staging.History).values(baseline_json=changed_baseline(s)))
        attempts.reserve(s)
    with s.session.begin():
        assert attempts.snapshot(s) == before


def test_initial_different_from_current_exact_v2_digests_and_readonly_replays(storage):
    s = storage
    from uuid import uuid4

    with s.session.begin():
        initial = changed_baseline(s)
        s.session.execute(sa.update(attempts.staging.History).values(baseline_json=initial))
    with s.session.begin():
        s.receipt = attempts.staging.enqueue(s)
        row = attempts.staging.stored(s)[0]
        payload = json.loads(row.desired_json)
        assert payload["schema_version"] == 2
        assert payload["desired"]["schema_version"] == 1
        assert payload["initial_baseline_schema_version"] == 1
        assert payload["initial_baseline_digest"] == digest(initial)
        assert row.scope_digest == digest(row.desired_json)
        assert row.idempotency_key == digest("casdoor-role-intent-v2:" + row.desired_json)
        assert s.session.scalar(sa.select(attempts.staging.History.baseline_json)) == initial
    statements = attempts.trace(s)
    with s.session.begin():
        assert not attempts.staging.enqueue(s).created
    assert not attempts.dml(statements)
    s.attempts = attempts.CasdoorRoleAttemptRepository(s.session)
    s.reservation_id = uuid4()
    with s.session.begin():
        first = attempts.reserve(s)
    statements.clear()
    with s.session.begin():
        assert attempts.reserve(s) == first
    assert not attempts.dml(statements)


@pytest.mark.parametrize("bad", ["local", "join_owner", "remote_owner"])
def test_canonical_initial_owner_or_local_refused_before_dml(storage, bad):
    s = storage
    baseline = json.loads(s.history.baseline_json)
    if bad == "local":
        baseline.update(backend="local", roles=[])
    elif bad == "join_owner":
        baseline["join_role"] = "owner"
    else:
        baseline["roles"] = [
            {
                "role_id": "owner-id",
                "is_builtin": True,
                "category": "global_system_default",
                "role_tag": "owner",
                "permission_keys": [],
            }
        ]
    initial = canonical(baseline)
    parse_role_baseline_json(initial)
    with s.session.begin():
        s.session.execute(sa.update(attempts.staging.History).values(baseline_json=initial))
    statements = attempts.trace(s)
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        attempts.staging.enqueue(s)
    assert not attempts.dml(statements)


@pytest.mark.parametrize("state", ["pristine", "reserved", "unknown", "in_flight"])
@pytest.mark.parametrize("payload_version", [1, 3, "malformed"])
def test_legacy_and_future_rows_preserved_and_blocked(staged, state, payload_version):
    s = staged
    if state == "reserved":
        with s.session.begin():
            attempts.reserve(s)
    with s.session.begin():
        row = attempts.staging.stored(s)[0]
        payload = json.loads(row.desired_json)
        payload.pop("initial_baseline_schema_version")
        payload.pop("initial_baseline_digest")
        payload["schema_version"] = payload_version
        text = "{" if payload_version == "malformed" else canonical(payload)
        values = dict(
            desired_json=text, scope_digest=digest(text), idempotency_key=digest("casdoor-role-intent-v1:" + text)
        )
        if state in ("unknown", "in_flight"):
            values["operation_state"] = CasdoorOperationState(state)
        s.session.execute(sa.update(Intent).values(**values))
        before = attempts.snapshot(s)
    statements = attempts.trace(s)
    for operation in (lambda: attempts.staging.enqueue(s), lambda: attempts.reserve(s)):
        with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
            operation()
    assert not attempts.dml(statements)
    with s.session.begin():
        assert attempts.snapshot(s) == before


def test_insert_trigger_rollback_with_valid_changed_initial_and_prior_identity(storage):
    from sqlalchemy.exc import IntegrityError

    s = storage
    with s.session.begin():
        before = attempts.snapshot(s)
        s.session.execute(
            sa.text(
                "CREATE TRIGGER reject_f11_insert BEFORE INSERT ON casdoor_sync_intent_extend "
                "BEGIN SELECT RAISE(ABORT, 'offline F11 trigger'); END"
            )
        )
    with pytest.raises(IntegrityError, match="offline F11 trigger"), s.session.begin():
        s.session.execute(sa.update(attempts.staging.Identity).values(profile_sync_json='{"caller":1}'))
        s.session.execute(sa.update(attempts.staging.History).values(baseline_json=changed_baseline(s)))
        attempts.staging.enqueue(s)
    with s.session.begin():
        assert attempts.snapshot(s) == before


def test_suppressed_reservation_trigger_rolls_back_prior_identity(staged):
    s = staged
    with s.session.begin():
        before = attempts.snapshot(s)
        s.session.execute(
            sa.text(
                "CREATE TRIGGER suppress_f11_update BEFORE UPDATE OF attempt_count ON casdoor_sync_intent_extend "
                "WHEN NEW.attempt_count = 1 BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    with pytest.raises(CasdoorRoleIntentConflict), s.session.begin():
        s.session.execute(sa.update(attempts.staging.Identity).values(profile_sync_json='{"caller":1}'))
        attempts.reserve(s)
    with s.session.begin():
        assert attempts.snapshot(s) == before
