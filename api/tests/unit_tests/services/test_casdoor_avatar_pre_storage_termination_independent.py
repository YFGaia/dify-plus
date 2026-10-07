"""Independent lifecycle checks for pre-storage avatar termination."""

import json

import sqlalchemy as sa
from models.casdoor_extend import CasdoorAuditExtend as Audit
from sqlalchemy.orm import Session
from test_casdoor_avatar_pre_storage_termination_extend import (
    actual_receipt,
    failure_source,
    row,
    run,
    snapshot,
    terminal_root,
)

pytest_plugins = ("test_casdoor_avatar_pre_storage_termination_extend",)


def test_actual_storage_readback_failure_cannot_seal_pre_storage_proof(consumer):
    s = consumer
    s.storage_fault = "read"

    result = run(s)

    assert result.code == "unknown"
    assert s.calls.count("save") == s.calls.count("readback") == s.calls.count("storage_close") == 1
    assert s.data  # The memory storage accepted bytes before its readback failed.
    value = row(s)
    assert value["operation_state"] == "unknown" and value["termination_state"] == "manual_recovery"
    assert value["error_code"] == "storage_unknown" and value["attempt_count"] == 1
    assert value["termination_proof_kind"] is None
    assert json.loads(value["desired_json"])["cleanup_state"] == "unknown"
    with Session(s.session.get_bind()) as reader:
        no_terminal_audit = sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_pre_storage")
        assert reader.scalar(no_terminal_audit) == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_finish")) == 1


def test_lost_terminal_commit_ack_needs_exact_fresh_reader_and_stays_unknown(consumer, monkeypatch):
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository

    s = consumer
    failure_source(s, monkeypatch, "error")
    original = CasdoorAvatarRepository.reconcile_pre_storage_failure
    observations = []

    def observe(repo, record, *, now):
        result = original(repo, record, now=now)
        observations.append((repo._session.consumer_index, result))
        return result

    monkeypatch.setattr(CasdoorAvatarRepository, "reconcile_pre_storage_failure", observe)

    def lose_terminal_ack(index):
        if index == 4:
            raise RuntimeError("synthetic terminal commit acknowledgement lost")

    s.commit_hook = lose_terminal_ack

    assert run(s).code == "unknown"

    assert observations == [(4, True), (5, True)]
    terminal = row(s)
    assert terminal["operation_state"] == "failed"
    assert terminal["termination_state"] == "confirmed"
    assert terminal["termination_proof_kind"] == "avatar_pre_storage"
    assert len(s.sessions) >= 6


def test_pre_storage_terminal_remains_closed_to_reclaim_finish_and_dispatch(consumer):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    committed = terminal_root(s, invocation, attempt, capability)
    assert committed.committed and not committed.failed
    before = snapshot(s)

    reclaim = s.consumer._root(
        invocation,
        lambda repo: repo.claim_and_reserve(s.intent_id, now=s.utc),
        write=True,
    )
    assert reclaim.clean and not reclaim.failed and reclaim.value.code == "closed"

    dispatch = s.consumer._root(invocation, lambda repo: repo._initial_dispatch_candidate(s.intent_id, now=s.utc))
    assert dispatch.clean and not dispatch.failed and dispatch.value is None

    retry = s.consumer._root(
        invocation,
        lambda repo: repo.finish_attempt(attempt, reason="fetch_failed", now=s.utc),
        write=True,
    )
    assert retry.failed and snapshot(s) == before
