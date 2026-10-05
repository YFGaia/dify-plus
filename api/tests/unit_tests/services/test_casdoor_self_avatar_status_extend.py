"""Actual SQL self observations of existing producers; no physical storage claims."""

import json
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from machinery.context import RequestContext
from models.account import Account
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfIdentityRepository, CasdoorSelfReadConflict
from services.casdoor_self_identity_service_extend import CasdoorSelfIdentityService
from sqlalchemy.orm import sessionmaker
from test_casdoor_avatar_attachment_extend import attach, attachment
from test_casdoor_avatar_consumer_extend import consumer, run
from test_casdoor_avatar_intent_extend import avatar as avatar_fixture
from test_casdoor_avatar_intent_extend import pending, storage_fixture
from test_casdoor_avatar_pre_storage_recovery_extend import produce
from test_casdoor_profile_repository_extend import NOW

avatar_fixture = avatar_fixture
storage_fixture = storage_fixture
consumer = consumer
attachment = attachment


def observe(s, *, now=NOW, account_id=None):
    service = CasdoorSelfIdentityService(session_factory=sessionmaker(s.session.get_bind()), rbac_enabled=False)
    context = RequestContext("synthetic-avatar-self", None, account_id or s.account.id, s.revision.default_workspace_id)
    return service.get_avatar_observations(context, now=now)


def one(s, **kwargs):
    return observe(s, **kwargs)["identities"][0]


def mutate(s, model, **values):
    with s.session.begin():
        s.session.execute(sa.update(model).values(**values))


def audit_mutation(s, change):
    with s.session.begin():
        audit = s.session.scalar(sa.select(Audit).where(Audit.action == "avatar_pre_storage"))
        value = json.loads(audit.summary_json)
        change(value)
        audit.summary_json = json.dumps(value, sort_keys=True, separators=(",", ":"))


def assert_unknown(value):
    assert value["avatar_status"] == "unknown"
    assert value["avatar_recorded_at"] is None
    assert value["avatar_last_reason"] is None
    assert value["avatar_recorded_generation"] is None


@pytest.mark.parametrize("enabled,expected", [(True, "no_record"), (False, "off")])
def test_current_policy_without_durable_work_does_not_invent_skip_reason(avatar_fixture, enabled, expected):
    s = avatar_fixture
    with s.session.begin():
        policy = json.loads(s.revision.policy_json)
        policy["avatar_sync"] = enabled
        s.session.execute(sa.update(Revision).values(policy_json=json.dumps(policy)))
    value = one(s)
    assert value["avatar_status"] == expected
    assert value["avatar_consistency"] == "current"
    assert value["avatar_recorded_at"] is None and value["avatar_last_reason"] is None


@pytest.mark.parametrize("seconds,expected", [(0, "pending"), (300, "source_expired")])
def test_actual_pending_has_source_expiry_and_sql_record_time(avatar_fixture, seconds, expected):
    s = avatar_fixture
    with s.session.begin():
        intent_id = pending(s).intent_id
    with s.session.begin():
        created_at = s.session.scalar(sa.select(Intent.created_at).where(Intent.id == str(intent_id)))
    value = one(s, now=NOW + timedelta(seconds=seconds))
    assert value["avatar_status"] == expected
    assert value["avatar_recorded_at"] == created_at.replace(tzinfo=UTC).isoformat(timespec="microseconds")
    assert value["avatar_recorded_generation"] == 1
    encoded = json.dumps(value)
    assert "url" not in encoded and "proof" not in encoded and "file_id" not in encoded and "ciphertext" not in encoded


def test_actual_claim_and_expired_lease_are_recorded_in_flight_not_terminal(consumer):
    s = consumer
    with s.session.begin():
        s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    assert one(s, now=NOW + timedelta(seconds=61))["avatar_status"] == "in_flight"


def test_actual_unknown_finish_is_diagnostic_not_confirmed_failure(consumer):
    s = consumer
    with s.session.begin():
        attempt = s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW).attempt
    with s.session.begin():
        s.avatar_owner.finish_attempt(attempt, reason="storage_unknown", now=NOW)
    value = one(s)
    assert value["avatar_status"] == "unknown" and value["avatar_last_reason"] == "storage_unknown"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")


def test_actual_t3_producer_is_confirmed_before_storage_only(consumer):
    s = consumer
    produce(s)
    value = one(s)
    assert value["avatar_status"] == "failed_before_storage"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")
    assert value["avatar_last_reason"] == "fetch_failed"
    assert not s.data and not any(call in s.calls for call in ("save", "readback"))


@pytest.mark.parametrize("fault", ["missing", "duplicate", "v1", "digest", "unknown_key", "oversize", "scalar"])
def test_t3_requires_exact_unique_bounded_v2_evidence(consumer, fault):
    s = consumer
    produce(s)
    if fault in ("missing", "duplicate", "oversize"):
        with s.session.begin():
            audit = s.session.scalar(sa.select(Audit).where(Audit.action == "avatar_pre_storage"))
            if fault == "missing":
                s.session.delete(audit)
            elif fault == "oversize":
                audit.summary_json = "x" * 2049
            else:
                values = {column.key: getattr(audit, column.key) for column in Audit.__table__.columns}
                values["id"] = str(uuid4())
                s.session.add(Audit(**values))
    elif fault == "scalar":
        mutate(s, Intent, updated_at=NOW.replace(tzinfo=None) + timedelta(microseconds=1))
    else:

        def change(value):
            if fault == "v1":
                value["schema_version"] = 1
            elif fault == "digest":
                value["terminal_intent_sha256"] = "0" * 64
            else:
                value["unknown_key"] = "raw-provider-value"

        audit_mutation(s, change)
    assert_unknown(one(s))


def test_actual_db_attachment_is_recorded_not_synced_and_local_edit_is_observed(attachment):
    s = attachment
    attach(s)
    value = one(s)
    assert value["avatar_status"] == "local_attachment_recorded"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")
    assert value["avatar_current_local_differs_from_last_applied"] is False
    mutate(s, Account, avatar=str(uuid4()))
    value = one(s)
    assert value["avatar_status"] == "local_override"
    assert value["avatar_current_local_differs_from_last_applied"] is True
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")


@pytest.mark.parametrize("fault", ["audit_missing", "file_owner", "file_missing", "proof", "readback", "storage_type"])
def test_applied_row_or_audit_alone_is_not_an_attachment_observation(attachment, fault):
    s = attachment
    attach(s)
    if fault == "audit_missing":
        with s.session.begin():
            s.session.execute(sa.delete(Audit).where(Audit.action == "avatar_attach"))
    elif fault == "file_missing":
        with s.session.begin():
            s.session.execute(sa.delete(UploadFile))
    elif fault == "file_owner":
        mutate(s, UploadFile, created_by=str(uuid4()))
    elif fault == "storage_type":
        with s.session.begin():
            s.session.execute(sa.text("UPDATE upload_files SET storage_type='invalid'"))
    elif fault == "proof":
        mutate(s, Intent, proof_ref=str(uuid4()))
    else:
        mutate(s, Intent, readback_at=NOW.replace(tzinfo=None) + timedelta(microseconds=1))
    assert_unknown(one(s))


@pytest.mark.parametrize("fault", ["generation", "fence", "disabled", "inactive"])
def test_recorded_attachment_is_historical_after_current_parent_changes(attachment, fault):
    s = attachment
    attach(s)
    if fault == "generation":
        mutate(s, Identity, sync_generation=2)
    elif fault == "fence":
        mutate(s, Namespace, fence_epoch=1)
    elif fault == "disabled":
        mutate(s, Integration, enabled=False)
    else:
        mutate(s, Namespace, lifecycle="archived")
    value = one(s)
    assert value["avatar_status"] == value["avatar_consistency"] == "historical"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")


@pytest.mark.parametrize("fault", ["oversize", "duplicate_key", "invalid_bool", "unknown_key"])
def test_invalid_current_policy_never_defaults_to_off(avatar_fixture, fault):
    s = avatar_fixture
    with s.session.begin():
        policy = json.loads(s.revision.policy_json)
        if fault == "invalid_bool":
            policy["avatar_sync"] = 0
        elif fault == "unknown_key":
            policy["claims"] = {}
        raw = json.dumps(policy)
        if fault == "oversize":
            raw = "x" * 4097
        elif fault == "duplicate_key":
            raw = raw[:-1] + ',"avatar_sync":false}'
        s.session.execute(sa.update(Revision).values(policy_json=raw))
    assert_unknown(one(s))


@pytest.mark.parametrize("fault", ["future_generation", "scope", "desired_oversize"])
def test_corrupt_scoped_intent_is_unknown_not_no_record(consumer, fault):
    s = consumer
    changes = (
        {"generation": 2}
        if fault == "future_generation"
        else {"scope_digest": "0" * 64}
        if fault == "scope"
        else {"desired_json": "x" * 16385}
    )
    mutate(s, Intent, **changes)
    assert_unknown(one(s))


def test_other_account_intent_is_not_enumerated(consumer):
    s = consumer
    mutate(s, Intent, account_id=str(uuid4()))
    assert one(s)["avatar_status"] == "no_record"


def test_same_generation_ambiguity_is_rejected_before_text_hydration(consumer):
    s = consumer
    with s.session.begin():
        row = s.session.get(Intent, str(s.intent_id))
        values = {column.key: getattr(row, column.key) for column in Intent.__table__.columns}
        values.update(id=str(uuid4()), idempotency_key="0" * 64, desired_json="x" * 20000)
        s.session.add(Intent(**values))
    assert_unknown(one(s))


@pytest.mark.parametrize("mode", ["transport_read_error", "storage_read_error"])
def test_actual_ambiguous_io_failure_stays_unknown_in_self_projection(consumer, mode):
    s = consumer
    if mode == "transport_read_error":
        s.http(mode="error")
    else:
        s.storage_fault = "read"
    assert run(s).code == "unknown"
    value = one(s)
    assert value["avatar_status"] == "unknown"
    assert value["avatar_last_reason"] == ("fetch_failed" if mode == "transport_read_error" else "storage_unknown")
    assert value["avatar_recorded_at"] is not None
    assert "synced" not in json.dumps(value)


def test_off_policy_does_not_erase_outstanding_operation(consumer):
    s = consumer
    with s.session.begin():
        policy = json.loads(s.revision.policy_json)
        policy["avatar_sync"] = False
        s.session.execute(sa.update(Revision).values(policy_json=json.dumps(policy)))
    assert one(s)["avatar_status"] == "pending"


def test_new_active_revision_does_not_upgrade_older_attachment_to_current(attachment):
    s = attachment
    attach(s)
    with s.session.begin():
        revision = s.session.get(Revision, s.revision.id)
        values = {column.key: getattr(revision, column.key) for column in Revision.__table__.columns}
        values.update(id=str(uuid4()), revision_number=revision.revision_number + 1)
        s.session.add(Revision(**values))
        s.session.flush()
        s.session.execute(sa.update(Integration).values(active_revision_id=values["id"]))
    value = one(s)
    assert value["avatar_status"] == value["avatar_consistency"] == "historical"
    assert value["avatar_recorded_at"] == NOW.isoformat(timespec="microseconds")


def test_actual_observation_statements_are_bounded_selects_without_secrets_or_io(consumer, monkeypatch):
    s = consumer
    calls = []

    def denied(*args, **kwargs):
        pytest.fail("self observation attempted decryption or I/O")

    monkeypatch.setattr(type(s.config_owner.crypto), "decrypt", denied)
    monkeypatch.setattr(s.consumer, "_consume_initial", denied)

    def capture(conn, cursor, statement, parameters, context, executemany):
        calls.append(statement.lower())

    engine = s.session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        assert one(s)["avatar_status"] == "pending"
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert calls and all(statement.lstrip().startswith("select") or statement == "begin" for statement in calls)
    calls = [statement for statement in calls if statement != "begin"]
    assert all("limit" in statement and "for update" not in statement for statement in calls)
    assert all("encrypted_secret" not in statement and "certificates_json" not in statement for statement in calls)
    candidate = [statement for statement in calls if "from casdoor_sync_intent_extend" in statement]
    assert candidate and all("account_id" in statement and "identity_id" in statement for statement in candidate)
    assert any("case when" in statement and "desired_json" in statement for statement in candidate)
    assert not s.calls


def test_actual_final_recheck_rejects_observed_intent_change(consumer, monkeypatch):
    s = consumer
    original = CasdoorSelfIdentityRepository.recheck

    def drift(reader):
        # Test-only concurrent-observation seam; production recheck remains SELECT-only.
        reader.session.execute(sa.update(Intent).values(updated_at=NOW.replace(tzinfo=None) + timedelta(seconds=1)))
        original(reader)

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "recheck", drift)
    with pytest.raises(CasdoorSelfReadConflict):
        observe(s)


def test_sql_failure_is_not_synthesized_as_no_record(consumer, monkeypatch):
    s = consumer
    from sqlalchemy.exc import OperationalError

    def fail(*args, **kwargs):
        raise OperationalError("synthetic select", {}, RuntimeError("offline SQL failure"))

    monkeypatch.setattr(CasdoorSelfIdentityRepository, "read", fail)
    with pytest.raises(CasdoorSelfReadConflict):
        observe(s)
