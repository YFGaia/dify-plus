"""Read-only authority on actual configured sessions and committed R1/R2 owners."""

import json
import re
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_casdoor_avatar_attachment_extend import attach, avatar_fixture, owner, snapshot, storage_fixture  # noqa: F401
from test_casdoor_avatar_attachment_extend import attachment as original_attachment
from test_casdoor_profile_repository_extend import NOW

from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict, _key
from repositories.casdoor_profile_repository_extend import _dump
from services.file_service import FileService

configured_attachment = original_attachment


@contextmanager
def readonly(s):
    """Observe actual statements; caller owns closing the clean explicit root."""
    writes = []

    def observe(connection, cursor, statement, parameters, context, many):
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.IGNORECASE):
            writes.append(statement)

    engine = s.maker.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        with s.maker() as session:
            transaction = session.begin()
            yield owner(s, session)
            assert session.get_transaction() is transaction and transaction.is_active
            assert not session.new and not session.dirty and not session.deleted
            session.rollback()
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert not writes and not s.io_calls


def second_claim(s):
    """Retain the accepted R2 managed-generation fixture and actual claim owner."""
    first = attach(s)
    with s.maker() as session, session.begin():
        previous = dict(session.execute(sa.select(*Intent.__table__.columns)).mappings().one())
        data = json.loads(previous["desired_json"])
        new_id = uuid4()
        identity = session.get(Identity, previous["identity_id"])
        sync = json.loads(identity.profile_sync_json)
        sync["generation"] = 2
        identity.sync_generation = 2
        identity.profile_sync_json = _dump(sync)
        data.update(generation=2, baseline=str(first.result_file_id), result_file_id=None, reservations=[])
        previous.update(
            id=str(new_id),
            generation=2,
            desired_json=_dump(data),
            idempotency_key=_key(UUID(previous["identity_id"]), 2, data["url_sha256"]),
            operation_state="pending",
            termination_state="not_started",
            attempt_count=0,
        )
        for key in (
            "resource_id",
            "attempt_id",
            "lease_owner",
            "lease_expires_at",
            "termination_proof_kind",
            "proof_ref",
            "readback_at",
            "terminated_at",
        ):
            previous[key] = None
        session.execute(sa.insert(Intent).values(**previous))
    with s.maker() as session, session.begin():
        claimed = owner(s, session).claim_and_reserve(new_id, now=NOW)
    assert claimed.code == "reserved"
    return claimed, first


def test_current_lease_source_reservation_frozen_and_zero_dml(configured_attachment, monkeypatch):
    s = configured_attachment
    monkeypatch.setattr(FileService, "insert_reserved_avatar", lambda *a: pytest.fail("preflight inserted"))
    with s.maker() as session, session.begin():
        session.execute(sa.update(Intent).values(lease_expires_at=(NOW + timedelta(seconds=42)).replace(tzinfo=None)))
    before = snapshot(s)
    with readonly(s) as repository:
        ready = repository.recheck_attempt_for_io(s.claimed.attempt, now=NOW)
        assert ready.attempt == s.claimed.attempt
        assert ready.source == s.claimed.source and ready.reservation == s.claimed.reservation
        assert ready.lease_expires_at == NOW + timedelta(seconds=42)
        assert ready.lease_expires_at.tzinfo is UTC
        assert ready.source.ciphertext not in repr(ready)
        assert ready.source.ciphertext not in repr(ready.source)
        with pytest.raises(FrozenInstanceError):
            ready.lease_expires_at = NOW
        outcome = repository.reconcile_intent_attachment(s.intent_id, now=NOW)
        assert outcome.code == "not_applied" and outcome.result_file_id is None
    assert snapshot(s) == before


@pytest.mark.parametrize("drift", ["join", "disabled", "generation", "watermark", "avatar", "unknown", "expiry", "ref"])
def test_invalid_authority_rolls_back_earlier_caller_write(configured_attachment, drift):
    s = configured_attachment
    with s.maker() as session, session.begin():
        if drift == "join":
            session.execute(sa.delete(TenantAccountJoin))
            session.add(TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id))
        elif drift == "disabled":
            session.execute(sa.update(Integration).values(enabled=False))
        elif drift == "generation":
            session.execute(sa.update(Identity).values(sync_generation=2))
        elif drift == "watermark":
            session.execute(sa.update(Identity).values(profile_sync_json="{}"))
        elif drift == "avatar":
            session.execute(sa.update(Account).values(avatar=str(uuid4())))
        elif drift == "unknown":
            owner(s, session).finish_attempt(s.claimed.attempt, reason="commit_unknown", now=NOW)
    before = snapshot(s)
    attempt = replace(s.claimed.attempt, attempt_id=uuid4()) if drift == "ref" else s.claimed.attempt
    now = NOW + timedelta(seconds=60) if drift == "expiry" else NOW
    with pytest.raises(CasdoorAvatarConflict, match="^casdoor_avatar_conflict$"), s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(name="earlier caller work"))
        session.flush()
        owner(s, session).recheck_attempt_for_io(attempt, now=now)
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["proof", "file", "late_proof", "late_file", "late_current"])
def test_managed_prior_and_query_callback_drift_rejected(configured_attachment, fault):
    s = configured_attachment
    claimed, first = second_claim(s)
    with readonly(s) as repository:
        assert repository.recheck_attempt_for_io(claimed.attempt, now=NOW).reservation == claimed.reservation
    mutation = (
        sa.update(Intent).where(Intent.id == str(s.intent_id)).values(proof_ref=None)
        if "proof" in fault
        else sa.update(UploadFile).where(UploadFile.id == str(first.result_file_id)).values(hash="f" * 64)
    )
    if fault == "late_current":
        mutation = sa.update(Account).values(avatar=str(uuid4()))
    if not fault.startswith("late_"):
        # A different valid hash is structurally acceptable without an earlier snapshot;
        # corrupt file ownership for the early strict-prior rejection case.
        if fault == "file":
            mutation = sa.update(UploadFile).values(used_by=str(uuid4()))
        with s.maker() as session, session.begin():
            session.execute(mutation)
    fired = []

    def during_latest(connection, cursor, statement, parameters, context, many):
        if not fired and statement.startswith(
            "SELECT casdoor_sync_intent_extend.id, casdoor_sync_intent_extend.generation"
        ):
            fired.append(True)
            connection.execute(mutation)

    engine = s.maker.kw["bind"]
    before = snapshot(s)
    if fault.startswith("late_"):
        sa.event.listen(engine, "before_cursor_execute", during_latest)
    try:
        with pytest.raises(CasdoorAvatarConflict), s.maker() as session, session.begin():
            session.execute(sa.update(Account).values(name="earlier caller work"))
            session.flush()
            owner(s, session).recheck_attempt_for_io(claimed.attempt, now=NOW)
    finally:
        if fault.startswith("late_"):
            sa.event.remove(engine, "before_cursor_execute", during_latest)
            assert fired
    assert snapshot(s) == before


@pytest.mark.parametrize("bad", ["proof", "used_float", "oversized", "lease", "nonapplied"])
def test_strict_intent_replay_never_promotes_weak_or_malformed_state(configured_attachment, bad):
    s = configured_attachment
    if bad != "nonapplied":
        attach(s)
    with s.maker() as session, session.begin():
        if bad == "proof":
            session.execute(sa.update(Intent).values(termination_proof_kind="legacy"))
        elif bad == "used_float":
            session.execute(sa.text("UPDATE upload_files SET used=1.5"))
        elif bad == "oversized":
            session.execute(sa.update(Intent).values(desired_json=" " * 20000))
        elif bad == "lease":
            session.execute(sa.update(Intent).values(lease_owner=None, lease_expires_at=None))
        else:
            session.execute(sa.text("UPDATE casdoor_sync_intent_extend SET attempt_count=1.5"))
    before = snapshot(s)
    with readonly(s) as repository:
        if bad == "proof":
            assert repository.claim_and_reserve(s.intent_id, now=NOW).code == "replayed"
        outcome = repository.reconcile_intent_attachment(s.intent_id, now=NOW)
        assert outcome.code == "unknown" and outcome.result_file_id is None
    assert snapshot(s) == before


def test_committed_strict_replay_survives_current_drift(configured_attachment, monkeypatch):
    s = configured_attachment
    first = attach(s)
    with s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(avatar=str(uuid4())))
        session.execute(sa.update(Integration).values(enabled=False))
        session.execute(sa.update(Identity).values(sync_generation=99))
        session.execute(sa.delete(TenantAccountJoin))
    monkeypatch.setattr(FileService, "insert_reserved_avatar", lambda *a: pytest.fail("replay inserted"))
    before = snapshot(s)
    with readonly(s) as repository:
        outcome = repository.reconcile_intent_attachment(s.intent_id, now=NOW + timedelta(days=1))
        assert outcome.code == "applied" and outcome.result_file_id == first.result_file_id
        with pytest.raises(CasdoorAvatarConflict):
            repository.recheck_attempt_for_io(s.claimed.attempt, now=NOW)
    assert snapshot(s) == before


@pytest.mark.parametrize("guard", ["autobegin", "nested", "dirty", "naive"])
def test_original_transaction_and_time_guards(configured_attachment, guard):
    s = configured_attachment
    with s.maker() as session:
        if guard == "autobegin":
            session.execute(sa.select(Intent.id))
        else:
            session.begin()
        if guard == "nested":
            session.begin_nested()
        elif guard == "dirty":
            session.get(Account, s.account.id).name = "unflushed"
        now = NOW.replace(tzinfo=None) if guard == "naive" else NOW
        repository = owner(s, session)
        with pytest.raises(CasdoorAvatarConflict):
            repository.recheck_attempt_for_io(s.claimed.attempt, now=now)
        with pytest.raises(CasdoorAvatarConflict):
            repository.reconcile_intent_attachment(s.intent_id, now=now)
        session.rollback()


def test_exact_inputs_and_bound_before_hydration(configured_attachment):
    s = configured_attachment
    with readonly(s) as repository:
        assert repository.reconcile_intent_attachment(str(s.intent_id), now=NOW).code == "unknown"
        assert repository.reconcile_intent_attachment(uuid4(), now=NOW).code == "unknown"
        with pytest.raises(CasdoorAvatarConflict):
            repository.recheck_attempt_for_io(replace(s.claimed.attempt, lease_owner="raw secret"), now=NOW)
    with s.maker() as session, session.begin():
        session.execute(sa.update(Intent).values(desired_json=" " * 20000))
    queries = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT") and "casdoor_sync_intent_extend.desired_json" in statement:
            queries.append(statement)

    engine = s.maker.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        with readonly(s) as repository:
            with pytest.raises(CasdoorAvatarConflict):
                repository.recheck_attempt_for_io(s.claimed.attempt, now=NOW)
            assert repository.reconcile_intent_attachment(s.intent_id, now=NOW).code == "unknown"
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert queries
    assert all("SELECT CAST(length(CAST(casdoor_sync_intent_extend.desired_json AS BLOB))" in q for q in queries)
