"""Actual native producer through mounted self GET; no cleanup authority in readers."""

import json
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa
from extensions.ext_storage import storage
from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend as Guard
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfIdentityRepository, CasdoorSelfReadConflict
from test_casdoor_avatar_cleanup_flow_extend import attachment_fault, reservation
from test_casdoor_avatar_cleanup_flow_extend import avatar_fixture as avatar_fixture
from test_casdoor_avatar_cleanup_flow_extend import consumer as consumer
from test_casdoor_avatar_cleanup_flow_extend import native as original_native
from test_casdoor_avatar_cleanup_flow_extend import storage_fixture as storage_fixture
from test_casdoor_avatar_cleanup_flow_extend import telemetry as telemetry
from test_casdoor_avatar_consumer_extend import row, run
from test_casdoor_avatar_retry_lineage_extend import prepare, produce
from test_casdoor_self_avatar_http_extend import mounted, response

native = original_native


@pytest.mark.parametrize("count", [1, 2])
def test_actual_native_completion_reaches_mounted_self_get(native, monkeypatch, count):
    s = native
    # Run after the original DB-only attachment cases in the finite manifest.
    # Their class-method denials must restore without instance-method shadows,
    # which the genuine native admission owner correctly refuses to trust.
    assert all(name not in storage.__dict__ for name in ("save", "load_stream", "delete", "exists"))
    if count == 2:
        produce(s)
        prepare(s)
        s.http()
    attachment_fault(s)
    assert run(s).code == "unknown"
    final = row(s)
    assert final["attempt_count"] == count
    assert final["termination_proof_kind"] == "avatar_cleanup"
    assert json.loads(final["desired_json"])["cleanup_state"] == "complete"
    assert not s.native.exists(reservation(s)["storage_key"])
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == "failed_storage_cleaned"
    assert value["avatar_last_reason"] == "attachment_lost"
    assert value["avatar_consistency"] == "current"
    assert value["avatar_current_local_differs_from_last_applied"] is None


def completed(s, count=1):
    if count == 2:
        produce(s)
        prepare(s)
        s.http()
    attachment_fault(s)
    assert run(s).code == "unknown"
    assert json.loads(row(s)["desired_json"])["cleanup_state"] == "complete"


def unknown(s, monkeypatch):
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == "unknown"
    assert value["avatar_recorded_at"] is None
    assert value["avatar_last_reason"] is None
    assert value["avatar_recorded_generation"] is None


@pytest.mark.parametrize("site", ["after_delete", "stat", "terminal_ack", "terminal_close", "completion_audit"])
def test_actual_pending_or_unobserved_cleanup_remains_unknown(native, monkeypatch, site):
    s = native
    attachment_fault(s)
    if site in ("terminal_ack", "terminal_close"):

        def loss(index):
            if index == 6:
                raise RuntimeError("synthetic terminal-root loss")

        if site == "terminal_ack":
            s.commit_hook = loss
        else:
            s.close_hook = loss
    elif site == "completion_audit":
        with s.session.begin():
            s.session.execute(
                sa.text(
                    "CREATE TRIGGER completion_fault AFTER INSERT ON casdoor_audit_extend "
                    "WHEN NEW.action='avatar_cleanup_complete' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END"
                )
            )
    previous = sys.getprofile()

    def profile(frame, event, arg):
        if getattr(arg, "__self__", None) is s.native.op:
            if site == "after_delete" and event == "c_return" and getattr(arg, "__name__", "") == "delete":
                raise RuntimeError("synthetic post-native-delete loss")
            if site == "stat" and event == "c_call" and getattr(arg, "__name__", "") == "stat":
                raise RuntimeError("synthetic native-stat loss")

    try:
        if site in ("after_delete", "stat"):
            sys.setprofile(profile)
        assert run(s).code == "unknown"
    finally:
        sys.setprofile(previous)
    assert json.loads(row(s)["desired_json"])["cleanup_state"] == "pending"
    unknown(s, monkeypatch)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize(
    "damage",
    [
        "cleanup_missing",
        "cleanup_duplicate",
        "completion_missing",
        "completion_duplicate",
        "fence_missing",
        "fence_duplicate",
        "pending_hash",
        "claim_hash",
        "fence_hash",
        "before_digest",
        "terminal_digest",
        "before_mutable",
        "references",
        "completion_hash",
        "audit_foreign",
        "audit_actor",
        "audit_time",
        "oversize",
        "duplicate_key",
        "guard_version",
        "guard_stage",
        "guard_intent",
        "guard_attempt",
        "guard_missing",
        "intent_time",
    ],
)
def test_actual_complete_proof_corruption_is_unknown_at_mounted_get(native, monkeypatch, count, damage):
    s = native
    completed(s, count)
    with s.maker() as session, session.begin():
        if damage.startswith("guard"):
            if damage == "guard_missing":
                session.execute(sa.delete(Guard).where(Guard.file_id == reservation(s)["file_id"]))
            else:
                key = damage.removeprefix("guard_")
                key = {"intent": "intent_id", "attempt": "attempt_id"}.get(key, key)
                value = 99 if key == "version" else "cleanup_pending" if key == "stage" else str(uuid4())
                session.execute(
                    sa.update(Guard).where(Guard.file_id == reservation(s)["file_id"]).values(**{key: value})
                )
        elif damage == "intent_time":
            session.execute(sa.update(Intent).where(Intent.id == str(s.intent_id)).values(readback_at=None))
        else:
            action = (
                "avatar_cleanup_complete"
                if damage.startswith("completion")
                else (
                    "avatar_reservation_fence"
                    if damage.startswith("fence")
                    else "avatar_pending"
                    if damage == "pending_hash"
                    else ("avatar_retry_claim" if count == 2 else "avatar_claim")
                    if damage == "claim_hash"
                    else "avatar_cleanup"
                )
            )
            where = [Audit.action == action]
            if action == "avatar_reservation_fence":
                where.append(Audit.correlation_id == reservation(s)["file_id"])
            audit = session.scalar(sa.select(Audit).where(*where))
            if damage.endswith("missing"):
                session.delete(audit)
            elif damage.endswith("duplicate"):
                values = {column.name: getattr(audit, column.name) for column in Audit.__table__.columns}
                session.add(Audit(**dict(values, id=str(uuid4()))))
            elif damage in ("pending_hash", "claim_hash", "fence_hash"):
                audit.result_code = "corrupt"
            elif damage == "audit_foreign":
                audit.account_id = str(uuid4())
            elif damage == "audit_actor":
                audit.actor_account_id = str(uuid4())
            elif damage == "audit_time":
                audit.created_at = audit.created_at.replace(year=2020)
            elif damage == "oversize":
                audit.summary_json = "x" * 8193
            elif damage == "duplicate_key":
                audit.summary_json = audit.summary_json[:-1] + ',"schema_version":1}'
            else:
                data = json.loads(audit.summary_json)
                if damage == "before_mutable":
                    data["before_mutable"][-1][1] = ["null"]
                elif damage == "references":
                    data["references"]["tenant_id"] = str(uuid4())
                else:
                    key = {
                        "before_digest": "before_sha256",
                        "terminal_digest": "terminal_sha256",
                        "completion_hash": "cleanup_sha256",
                    }[damage]
                    data[key] = "0" * 64
                audit.summary_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    unknown(s, monkeypatch)


def test_actual_completed_cleanup_remains_historical_after_generation_change(native, monkeypatch):
    s = native
    completed(s)
    with s.maker() as session, session.begin():
        session.execute(sa.update(Identity).values(sync_generation=2))
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == value["avatar_consistency"] == "historical"
    assert value["avatar_last_reason"] == "attachment_lost"


@pytest.mark.parametrize("model", [Audit, Guard])
def test_actual_final_readback_tracks_cleanup_audit_and_guard_changes(native, monkeypatch, model):
    s = native
    completed(s)
    original = CasdoorSelfIdentityRepository.recheck

    def recheck(reader):
        with s.maker() as session, session.begin():
            if model is Guard:
                session.execute(sa.update(Guard).values(version=99))
            else:
                session.execute(
                    sa.update(Audit).where(Audit.action == "avatar_cleanup_complete").values(result_code="corrupt")
                )
        with pytest.raises(CasdoorSelfReadConflict):
            original(reader)
        raise CasdoorSelfReadConflict()

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "recheck", recheck)
    http = mounted(s, monkeypatch)
    result = http.client.get("/console/api/account/casdoor-identity", headers=http.headers)
    assert result.status_code == 503


@pytest.mark.parametrize("site", ["ack", "close"])
def test_actual_durable_complete_proof_is_readable_after_final_ack_or_close_loss(native, monkeypatch, site):
    s = native
    attachment_fault(s)

    def loss(index):
        if index == 8:
            raise RuntimeError("synthetic completion-root loss")

    if site == "ack":
        s.commit_hook = loss
    else:
        s.close_hook = loss
    assert run(s).code == "unknown"
    assert json.loads(row(s)["desired_json"])["cleanup_state"] == "complete"
    assert not s.native.exists(reservation(s)["storage_key"])
    before = row(s)
    statements = []

    def selected(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT") or statement.strip().upper() == "BEGIN"
        assert "FOR UPDATE" not in statement.upper()

    engine = s.session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", selected)
    try:
        value = response(mounted(s, monkeypatch)).json["identities"][0]
    finally:
        sa.event.remove(engine, "before_cursor_execute", selected)
    assert statements
    assert value["avatar_status"] == "failed_storage_cleaned"
    assert run(s).code == "unknown"
    assert row(s) == before


@pytest.mark.parametrize("field", ["intent_id", "attempt_id"])
def test_actual_complete_guard_uuid_is_bounded_before_hydration(native, monkeypatch, field):
    s = native
    completed(s)
    with s.maker() as session, session.begin():
        session.execute(sa.update(Guard).values(**{field: "x" * 8193}))
    lengths = []
    original = CasdoorSelfIdentityRepository.read

    def read(reader, statement):
        rows = original(reader, statement)
        if Guard.__tablename__ in str(statement):
            lengths.extend(len(value[field]) for value in rows if isinstance(value.get(field), str))
        return rows

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "read", read)
    unknown(s, monkeypatch)
    assert not lengths or max(lengths) <= 64


@pytest.mark.parametrize("count", [1, 2])
def test_actual_original_disable_preserves_completed_cleanup_history(native, monkeypatch, count):
    s = native
    completed(s, count)
    with s.session.begin():
        etag = s.session.scalar(sa.select(Integration.etag))
        disabled = s.config_owner.disable(etag=etag, actor_account_id=uuid4())
        assert disabled.reconciliation_required is False
    value = response(mounted(s, monkeypatch)).json["identities"][0]
    assert value["avatar_status"] == value["avatar_consistency"] == "historical"


def test_actual_current_count2_malformed_scope_stays_unknown(native, monkeypatch):
    s = native
    completed(s, 2)
    with s.maker() as session, session.begin():
        session.execute(sa.update(Identity).values(subject="tampered subject"))
    unknown(s, monkeypatch)


def test_actual_historical_count2_still_requires_immutable_completion(native, monkeypatch):
    s = native
    completed(s, 2)
    with s.session.begin():
        etag = s.session.scalar(sa.select(Integration.etag))
        s.config_owner.disable(etag=etag, actor_account_id=uuid4())
    with s.maker() as session, session.begin():
        session.execute(sa.delete(Audit).where(Audit.action == "avatar_cleanup_complete"))
    unknown(s, monkeypatch)
