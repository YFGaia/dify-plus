"""Normal pre-storage owner failure, opaque proof and exact transactional retention."""

import copy
import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from core.casdoor import avatar_termination as termination
from core.casdoor.crypto import CasdoorCrypto
from core.file import remote_fetcher
from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    _worker_state,
)
from repositories.casdoor_profile_repository_extend import _dump
from services.casdoor_avatar_consumer_service_extend import (
    _Budget,
    _Invocation,
    _RootResult,
)
from services.file_service import AvatarNormalization, FileService
from sqlalchemy.orm import Session
from test_casdoor_avatar_attempt_extend import snapshot
from test_casdoor_avatar_consumer_extend import (
    avatar_fixture,
    row,
    run,
    storage_fixture,
)
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_profile_repository_extend import NOW

avatar_fixture = avatar_fixture
storage_fixture = storage_fixture
consumer = original_consumer


def failure_source(s, monkeypatch, mode):
    if mode == "proxy":
        for name in ("SSRF_PROXY_ALL_URL", "SSRF_PROXY_HTTP_URL", "SSRF_PROXY_HTTPS_URL"):
            monkeypatch.setattr(remote_fetcher.dify_config, name, "")
    elif mode == "image":
        s.http(content=b"invalid-image")
    else:

        def supplier_failure(*args, **kwargs):
            raise ValueError("synthetic local supplier failure")

        monkeypatch.setattr(CasdoorCrypto, "decrypt", supplier_failure)


def actual_receipt(s):
    """Use the actual committed root/claim/preflight and actual normal fetch owner."""
    invocation = _Invocation()
    claimed = s.consumer._root(invocation, lambda repo: repo.claim_and_reserve(s.intent_id, now=s.utc), write=True)
    assert claimed.committed and claimed.clean and not claimed.failed
    checked = s.consumer._root(invocation, lambda repo: repo.recheck_attempt_for_io(claimed.value.attempt, now=s.utc))
    assert checked.clean and not checked.failed
    tracker = termination._start_pre_storage(invocation, claimed, checked.value)
    budget = _Budget(
        s.consumer._now, s.consumer._monotonic, checked.value.source.expires_at, checked.value.lease_expires_at
    )

    def supplier_failure():
        raise ValueError("synthetic local supplier failure")

    fetched = termination._fetch_before_storage(tracker, supplier_failure, budget)
    assert (fetched.status, fetched.reason, fetched.termination) == ("failed", "supplier_failed", "confirmed")
    capability = termination._seal_pre_storage(tracker)
    assert capability is not None
    return invocation, claimed.value.attempt, tracker, capability


def terminal_root(s, invocation, attempt, capability):
    return s.consumer._root(
        invocation, lambda repo: repo.confirm_pre_storage_failure(attempt, capability, now=s.utc), write=True
    )


def assert_terminal(s, reason):
    value = row(s)
    assert value["operation_state"] == "failed" and value["termination_state"] == "confirmed"
    assert value["termination_proof_kind"] == "avatar_pre_storage"
    assert str(UUID(value["proof_ref"])) == value["proof_ref"]
    assert value["error_code"] == reason and value["attempt_count"] == 1
    assert value["terminated_at"] == value["updated_at"] == s.utc.replace(tzinfo=None)
    assert all(value[name] is None for name in ("retry_at", "resource_id", "sent_at", "acknowledged_at", "readback_at"))
    data = _worker_state(value)
    assert data["cleanup_state"] == data["reservations"][0]["cleanup_state"] == "complete"
    assert data["result_file_id"] is None
    assert not any(call in s.calls for call in ("save", "readback", "storage_close")) and not s.data
    with Session(s.session.get_bind()) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(UploadFile)) == 0
        audits = list(reader.scalars(sa.select(Audit).where(Audit.action == "avatar_pre_storage")))
        assert len(audits) == 1
        summary = json.loads(audits[0].summary_json)
        assert audits[0].result_code == "failed" and summary["reason"] == reason and summary["count"] == 1
        assert set(summary["references"]) == {
            "namespace_id",
            "revision_id",
            "identity_id",
            "account_id",
            "intent_id",
            "attempt_id",
            "file_id",
        }
        assert summary["references"]["file_id"] == data["reservations"][0]["file_id"]
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_finish")) == 0
    return value


@pytest.mark.parametrize("mode", ["proxy", "error", "image"], ids=["proxy", "error", "image"])
def test_normal_owner_failure_is_confirmed_but_public_result_unknown(consumer, monkeypatch, mode):
    s = consumer
    before = row(s)
    failure_source(s, monkeypatch, mode)
    assert run(s).code == "unknown"
    value = assert_terminal(s, {"proxy": "fetch_rejected", "error": "fetch_failed", "image": "image_rejected"}[mode])
    old, new = json.loads(before["desired_json"]), json.loads(value["desired_json"])
    assert {key: item for key, item in old.items() if key not in ("cleanup_state", "reservations")} == {
        key: item for key, item in new.items() if key not in ("cleanup_state", "reservations")
    }
    assert all(session.consumer_closed and not session.in_transaction() for session in s.sessions)
    if mode == "error":
        assert "http" not in s.calls
    calls = list(s.calls)
    assert run(s).code == "unknown" and s.calls == calls
    assert row(s) == value


@pytest.mark.parametrize(
    "case",
    ["fetch-copy", "fetch-throw", "image-copy", "image-throw"],
    ids=["fetch-copy", "fetch-throw", "image-copy", "image-throw"],
)
def test_untrusted_results_and_thrown_owner_errors_never_seal(consumer, monkeypatch, case):
    s = consumer

    def thrown(*args, **kwargs):
        raise RuntimeError("synthetic owner error")

    if case == "fetch-copy":
        result = remote_fetcher.BoundedExternalFile("failed", "request_failed")
        monkeypatch.setattr(remote_fetcher, "fetch_bounded_external_file", lambda *args: copy.copy(result))
    elif case == "fetch-throw":
        monkeypatch.setattr(remote_fetcher, "fetch_bounded_external_file", thrown)
    elif case == "image-copy":
        monkeypatch.setattr(FileService, "normalize_avatar_image", lambda *args: AvatarNormalization("invalid_image"))
    else:
        monkeypatch.setattr(FileService, "normalize_avatar_image", thrown)
    assert run(s).code == "unknown"
    value = row(s)
    assert value["operation_state"] == "unknown" and value["termination_state"] == "manual_recovery"
    assert value["termination_proof_kind"] is None and not s.data


@pytest.mark.parametrize("mode", ["cancel", "close_error", "timeout"], ids=["cancel", "close-error", "timeout"])
def test_cancel_unconfirmed_and_actual_deadline_never_confirm(consumer, mode):
    s = consumer
    s.http(mode="wait" if mode == "timeout" else mode)
    if mode == "timeout":
        s.utc = NOW + timedelta(seconds=299.75)
    assert run(s).code == "unknown"
    value = row(s)
    assert value["operation_state"] == "unknown" and value["termination_state"] == "manual_recovery"
    assert value["termination_proof_kind"] is None
    assert not s.data and "save" not in s.calls


def test_forged_copied_receipts_and_claim_results_cannot_mint_or_replay(consumer):
    s = consumer
    invocation = _Invocation()
    claimed = s.consumer._root(invocation, lambda repo: repo.claim_and_reserve(s.intent_id, now=s.utc), write=True)
    checked = s.consumer._root(invocation, lambda repo: repo.recheck_attempt_for_io(claimed.value.attempt, now=s.utc))
    for fake in (replace(claimed), _RootResult(value=replace(claimed.value), committed=True, commit_attempted=True)):
        with pytest.raises(ValueError, match="avatar_termination_invalid"):
            termination._start_pre_storage(invocation, fake, checked.value)
    tracker = termination._start_pre_storage(invocation, claimed, checked.value)
    budget = _Budget(
        s.consumer._now, s.consumer._monotonic, checked.value.source.expires_at, checked.value.lease_expires_at
    )

    def supplier_failure():
        raise ValueError("synthetic local supplier failure")

    fetched = termination._fetch_before_storage(tracker, supplier_failure, budget)
    assert fetched.status == "failed"
    capability = termination._seal_pre_storage(tracker)
    assert termination._seal_pre_storage(tracker) is None
    with pytest.raises(TypeError):
        termination._PreStorageCapability()
    with pytest.raises(TypeError):
        copy.copy(capability)
    before = snapshot(s)
    for fake in ("fetch_failed", fetched, object.__new__(termination._PreStorageCapability)):
        result = terminal_root(s, invocation, claimed.value.attempt, fake)
        assert result.failed and snapshot(s) == before
    result = terminal_root(s, invocation, claimed.value.attempt, capability)
    assert result.committed and not result.failed
    assert_terminal(s, "fetch_failed")
    before = snapshot(s)
    replay = terminal_root(s, invocation, claimed.value.attempt, capability)
    assert replay.failed and snapshot(s) == before


@pytest.mark.parametrize("case", ["copy", "foreign"], ids=["copy", "foreign"])
def test_receipt_rejects_wrong_attempt_identity(consumer, case):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    wrong = replace(attempt) if case == "copy" else replace(attempt, attempt_id=uuid4())
    before = snapshot(s)
    result = terminal_root(s, invocation, wrong, capability)
    assert result.failed and snapshot(s) == before
    assert terminal_root(s, invocation, attempt, capability).failed
    assert termination._seal_pre_storage(tracker) is None


def test_storage_entered_marker_irrevocably_revokes_receipt(consumer):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    termination._enter_avatar_storage(tracker)
    assert termination._seal_pre_storage(tracker) is None
    before = snapshot(s)
    assert terminal_root(s, invocation, attempt, capability).failed
    assert snapshot(s) == before and not s.data


@pytest.mark.parametrize(
    "drift",
    ["parent", "lease", "reservation", "generation", "policy", "terminal"],
    ids=["parent", "lease", "reservation", "generation", "policy", "terminal"],
)
def test_current_authority_and_exact_reservation_drift_deny_terminal_write(consumer, drift):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    with s.session.begin():
        if drift == "parent":
            s.session.execute(sa.delete(TenantAccountJoin))
        elif drift == "lease":
            s.session.execute(
                sa.update(Intent).values(lease_expires_at=(NOW - timedelta(seconds=1)).replace(tzinfo=None))
            )
        elif drift == "generation":
            s.session.execute(sa.update(Identity).values(sync_generation=2))
        elif drift == "policy":
            s.session.execute(sa.update(Revision).values(policy_json="{}"))
        else:
            value = dict(s.session.execute(sa.select(*Intent.__table__.columns)).mappings().one())
            data = json.loads(value["desired_json"])
            if drift == "reservation":
                replacement = str(uuid4())
                data["reservations"][0]["file_id"] = replacement
                data["reservations"][0]["storage_key"] = (
                    f"casdoor-avatar/{s.intent_id}/{attempt.attempt_id}/{replacement}.png"
                )
                changes = {}
            else:
                data["cleanup_state"] = data["reservations"][0]["cleanup_state"] = "unknown"
                changes = dict(
                    operation_state="unknown", termination_state="manual_recovery", error_code="fetch_unknown"
                )
            s.session.execute(sa.update(Intent).values(desired_json=_dump(data), **changes))
    before = snapshot(s)
    assert terminal_root(s, invocation, attempt, capability).failed
    assert snapshot(s) == before and not s.data
    assert termination._seal_pre_storage(tracker) is None


@pytest.mark.parametrize(
    "fault",
    ["audit-abort", "audit-tamper", "proof-tamper", "parent-tamper", "final-read"],
    ids=["audit-abort", "audit-tamper", "proof-tamper", "parent-tamper", "final-read"],
)
def test_proof_audit_and_final_reread_fault_roll_back_entire_root(consumer, monkeypatch, fault):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    if fault != "final-read":
        statements = {
            "audit-abort": "SELECT RAISE(ABORT, 'synthetic terminal fault');",
            "audit-tamper": "UPDATE casdoor_audit_extend SET summary_json = 'corrupt' WHERE id = NEW.id;",
            "proof-tamper": "UPDATE casdoor_sync_intent_extend SET proof_ref = 'corrupt';",
            "parent-tamper": "UPDATE casdoor_identity_extend SET sync_generation = sync_generation + 1;",
        }
        with s.session.begin():
            s.session.execute(
                sa.text(
                    "CREATE TRIGGER terminal_fault AFTER INSERT ON casdoor_audit_extend "
                    "WHEN NEW.action='avatar_pre_storage' BEGIN " + statements[fault] + " END"
                )
            )
    else:
        from repositories.casdoor_avatar_repository_extend import (
            CasdoorAvatarRepository,
        )

        original = CasdoorAvatarRepository._worker_root
        fired = []

        def mutate_after_snapshot(repo, *args, **kwargs):
            root = original(repo, *args, **kwargs)
            if root["intent"]["termination_proof_kind"] == "avatar_pre_storage" and not fired:
                fired.append(True)
                repo._session.execute(sa.update(Intent).values(proof_ref=str(uuid4())))
            return root

        monkeypatch.setattr(CasdoorAvatarRepository, "_worker_root", mutate_after_snapshot)
    before = snapshot(s)

    def operation(repo):
        repo._session.execute(sa.update(Account).values(name="earlier caller write"))
        repo._session.flush()
        return repo.confirm_pre_storage_failure(attempt, capability, now=s.utc)

    result = s.consumer._root(invocation, operation, write=True)
    assert result.failed and not result.commit_attempted
    assert snapshot(s) == before and not s.data
    assert termination._seal_pre_storage(tracker) is None
    if fault == "final-read":
        assert fired


def test_terminal_shape_independently_rejects_malformed_proof(consumer):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    assert terminal_root(s, invocation, attempt, capability).committed
    original = assert_terminal(s, "fetch_failed")
    changes = (
        {"proof_ref": "not-a-uuid"},
        {"attempt_count": 2},
        {"operation_state": "cancelled"},
        {"error_code": "storage_unknown"},
        {"retry_at": NOW.replace(tzinfo=None)},
        {"terminated_at": (NOW + timedelta(seconds=61)).replace(tzinfo=None)},
    )
    for change in changes:
        with pytest.raises(CasdoorAvatarConflict):
            _worker_state(dict(original, **change))
    data = json.loads(original["desired_json"])
    data["reservations"][0]["cleanup_state"] = "none"
    with pytest.raises(CasdoorAvatarConflict):
        _worker_state(dict(original, desired_json=_dump(data)))
    assert termination._seal_pre_storage(tracker) is None


@pytest.mark.parametrize(
    "fault", ["lost-ack", "close", "read-close", "read-drift"], ids=["lost-ack", "close", "read-close", "read-drift"]
)
def test_terminal_commit_uncertainty_uses_fresh_exact_reader_and_stays_public_unknown(consumer, monkeypatch, fault):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository

    s = consumer
    failure_source(s, monkeypatch, "error")
    original = CasdoorAvatarRepository.reconcile_pre_storage_failure
    observed = []

    def observe(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        observed.append((repo._session.consumer_index, result))
        return result

    monkeypatch.setattr(CasdoorAvatarRepository, "reconcile_pre_storage_failure", observe)

    def commit(index):
        if index == 4 and fault == "lost-ack":
            raise RuntimeError("synthetic terminal ACK lost")
        if index == 4 and fault == "read-drift":
            with Session(s.session.get_bind()) as session, session.begin():
                session.execute(sa.update(Intent).values(proof_ref=str(uuid4()), updated_at=s.utc))

    def close(index):
        if (fault == "close" and index == 4) or (fault == "read-close" and index == 5):
            raise RuntimeError("synthetic terminal close error")

    s.commit_hook, s.close_hook = commit, close
    assert run(s).code == "unknown"
    assert (5, fault != "read-drift") in observed
    assert len(s.sessions) == 6 and not s.data
    assert_terminal(s, "fetch_failed")
    assert all(session.consumer_closed and not session.in_transaction() for session in s.sessions)


def test_terminal_state_remains_excluded_from_initial_claim_dispatch_and_reason_finish(consumer):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    assert terminal_root(s, invocation, attempt, capability).committed
    before = snapshot(s)
    checked = s.consumer._root(invocation, lambda repo: repo.claim_and_reserve(s.intent_id, now=s.utc), write=True)
    assert checked.value.code == "closed"
    checked = s.consumer._root(invocation, lambda repo: repo._initial_dispatch_candidate(s.intent_id, now=s.utc))
    assert checked.value is None and not checked.failed
    checked = s.consumer._root(
        invocation, lambda repo: repo.finish_attempt(attempt, reason="fetch_failed", now=s.utc), write=True
    )
    assert checked.failed and snapshot(s) == before
    assert termination._seal_pre_storage(tracker) is None


def test_terminal_owner_requires_clean_explicit_root_before_consuming_capability(consumer):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository

    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    with s.maker() as session:
        repo = CasdoorAvatarRepository(
            session, configuration_repository=s.consumer._configuration_service._repository(session)
        )
        with pytest.raises(CasdoorAvatarConflict):
            repo.confirm_pre_storage_failure(attempt, capability, now=s.utc)
        with session.begin():
            with session.begin_nested(), pytest.raises(CasdoorAvatarConflict):
                repo.confirm_pre_storage_failure(attempt, capability, now=s.utc)
    assert terminal_root(s, invocation, attempt, capability).committed
    assert_terminal(s, "fetch_failed")
    assert termination._seal_pre_storage(tracker) is None


@pytest.mark.parametrize(
    "case",
    ["read-error", "read-timeout", "connect-timeout", "supplier-builtin", "supplier-read", "supplier-connect"],
    ids=["read-error", "read-timeout", "connect-timeout", "supplier-builtin", "supplier-read", "supplier-connect"],
)
def test_actual_ambiguous_transport_and_supplier_timeout_always_unknown(consumer, monkeypatch, case):
    s = consumer
    exception_type = {
        "read-error": httpx.ReadError,
        "read-timeout": httpx.ReadTimeout,
        "connect-timeout": httpx.ConnectTimeout,
        "supplier-builtin": TimeoutError,
        "supplier-read": httpx.ReadTimeout,
        "supplier-connect": httpx.ConnectTimeout,
    }[case]
    if case.startswith("supplier-"):

        def supplier_timeout(*args, **kwargs):
            raise exception_type("synthetic supplier timeout")

        monkeypatch.setattr(CasdoorCrypto, "decrypt", supplier_timeout)
    else:

        async def failing_stream(body):
            raise exception_type("synthetic transport error")
            yield b""

        monkeypatch.setattr(type(s.transport.body), "__aiter__", failing_stream)
    assert run(s).code == "unknown"
    value = row(s)
    assert value["operation_state"] == "unknown" and value["termination_state"] == "manual_recovery"
    assert value["termination_proof_kind"] is value["proof_ref"] is None
    assert not s.data and not any(call in s.calls for call in ("save", "readback"))
    if case.startswith("supplier-"):
        assert "http" not in s.calls
    else:
        assert s.transport.closed and s.transport.body.closed
    with Session(s.session.get_bind()) as reader:
        assert (
            reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_pre_storage"))
            == 0
        )
        assert reader.scalar(sa.select(sa.func.count()).select_from(UploadFile)) == 0
