"""Independent checks for consumer transaction cleanup boundaries."""

from uuid import UUID

import pytest
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_avatar_consumer_extend import row, run, unknown
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_profile_repository_extend import storage as original_storage

storage = original_storage
consumer = original_consumer
avatar = original_avatar


@pytest.fixture(name="storage_fixture")
def actual_storage_fixture(storage):
    return storage


@pytest.fixture(name="avatar_fixture")
def actual_avatar_fixture(avatar):
    return avatar


def test_claim_commit_ack_with_close_fault_stops_before_external_io(consumer):
    s = consumer

    def close_fault(index):
        if index == 2:
            raise RuntimeError("ordinary close failure")

    s.close_hook = close_fault
    result = run(s)

    assert result.code == "unknown"
    assert not s.calls
    unknown(s, "commit_unknown")
    assert len(s.sessions) == 3
    assert row(s)["operation_state"] == "unknown"


def test_attach_ack_reader_close_fault_is_unknown_then_strict_replay_is_io_free(consumer):
    s = consumer

    def close_fault(index):
        if index == 6:
            raise RuntimeError("ack reader close failure")

    s.close_hook = close_fault
    first = run(s)

    assert first.code == "unknown"
    assert row(s)["operation_state"] == "applied"
    assert s.calls.count("save") == 1
    assert s.calls.count("decrypt") == s.calls.count("http") == 1
    after_attach = s.calls.copy()

    s.close_hook = None
    replay = run(s)

    assert replay.code == "applied" and isinstance(replay.file_id, UUID)
    assert replay.file_id == UUID(row(s)["resource_id"])
    assert s.calls == after_attach
    assert s.calls.count("save") == 1
