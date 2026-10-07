"""Actual original claims and native cleanup, with corrupted/ref-bearing SQL refusal cases."""

import json
from datetime import datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_avatar_file_guard_extend import (
    CasdoorAvatarFileGuardExtend as Guard,
)
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.human_input import HumanInputFormUploadFile
from models.model import MessageFile, UploadFile
from models.tools import ToolFile
from models.workflow import WorkflowDraftVariableFile, WorkflowNodeExecutionOffload
from test_casdoor_avatar_cleanup_flow_extend import (
    after_store,
    assert_retained,
    attachment_fault,
    avatar_fixture,
    consumer,
    native,
    reservation,
    row,
    run,
    storage_fixture,
    telemetry,
)

avatar_fixture = avatar_fixture
consumer = consumer
native = native
storage_fixture = storage_fixture
telemetry = telemetry


def insert_ref(s, kind):
    target = reservation(s)
    ids = dict(id=str(uuid4()), tenant_id=str(uuid4()), app_id=str(uuid4()))
    with s.maker() as session, session.begin():
        if kind.startswith("upload"):
            values = dict(
                id=target["file_id"] if kind == "upload_id" else str(uuid4()),
                tenant_id=str(uuid4()),
                storage_type="local",
                key=target["storage_key"] if kind == "upload_key" else "upload_files/unrelated",
                name="unrelated",
                size=1,
                extension="png",
                mime_type="image/png",
                created_by_role="account",
                created_by=str(uuid4()),
                created_at=datetime(2026, 9, 30),
                used=False,
            )
            session.execute(sa.insert(UploadFile.__table__).values(**values))
        elif kind.startswith("tool"):
            session.execute(
                sa.insert(ToolFile.__table__).values(
                    id=target["file_id"] if kind == "tool_unrelated_id" else str(uuid4()),
                    user_id=str(uuid4()),
                    tenant_id=str(uuid4()),
                    conversation_id=None,
                    file_key="tools/unrelated" if kind == "tool_unrelated_id" else target["storage_key"],
                    mimetype="image/png",
                )
            )
        elif kind == "message":
            session.execute(
                sa.insert(MessageFile.__table__).values(
                    id=str(uuid4()),
                    message_id=str(uuid4()),
                    type="image",
                    transfer_method="local_file",
                    created_by_role="account",
                    created_by=str(uuid4()),
                    upload_file_id=target["file_id"],
                )
            )
        elif kind == "message_url":
            session.execute(
                sa.insert(MessageFile.__table__).values(
                    id=str(uuid4()),
                    message_id=str(uuid4()),
                    type="image",
                    transfer_method="remote_url",
                    created_by_role="account",
                    created_by=str(uuid4()),
                    url=f"https://files.example.invalid/files/{target['file_id']}/file-preview",
                )
            )
        elif kind == "human":
            session.execute(
                sa.insert(HumanInputFormUploadFile.__table__).values(
                    **ids,
                    form_id=str(uuid4()),
                    upload_file_id=target["file_id"],
                    upload_token_id=str(uuid4()),
                )
            )
        elif kind == "draft":
            session.execute(
                sa.insert(WorkflowDraftVariableFile.__table__).values(
                    **ids,
                    user_id=str(uuid4()),
                    upload_file_id=target["file_id"],
                    size=1,
                )
            )
        else:
            session.execute(
                sa.insert(WorkflowNodeExecutionOffload.__table__).values(
                    **ids, type="inputs", file_id=target["file_id"]
                )
            )


@pytest.mark.parametrize(
    "kind",
    [
        "upload_id",
        "upload_key",
        "tool",
        "message",
        "message_url",
        "human",
        "draft",
        "offload",
    ],
)
def test_every_actual_global_typed_or_key_reference_vetoes_physical_delete(native, kind):
    s = native
    attachment_fault(s)
    after_store(s, lambda: insert_ref(s, kind))
    assert run(s).code == "unknown"
    assert_retained(s)


def test_tool_uuid_alone_does_not_own_the_reserved_physical_key(native):
    s = native
    attachment_fault(s)
    after_store(s, lambda: insert_ref(s, "tool_unrelated_id"))
    run(s)
    assert row(s)["termination_proof_kind"] == "avatar_cleanup"
    assert s.native.exists(reservation(s)["storage_key"]) is False


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize(
    "damage",
    [
        "fence_missing",
        "fence_duplicate",
        "fence_hash",
        "claim_hash",
        "pending_hash",
        "guard_missing",
        "guard_wrong",
        "fence_oversize",
    ],
)
def test_real_original_lineage_damage_refuses_cleanup_without_repair(native, count, damage):
    from test_casdoor_avatar_retry_lineage_extend import prepare, produce

    s = native
    if count == 2:
        produce(s)
        prepare(s)
        s.http()
    attachment_fault(s)

    def corrupt():
        target = reservation(s)
        with s.maker() as session, session.begin():
            if damage.startswith("guard"):
                if damage == "guard_missing":
                    session.execute(sa.delete(Guard).where(Guard.file_id == target["file_id"]))
                else:
                    session.execute(
                        sa.update(Guard).where(Guard.file_id == target["file_id"]).values(attempt_id=str(uuid4()))
                    )
                return
            action = (
                "avatar_pending"
                if damage == "pending_hash"
                else ("avatar_retry_claim" if count == 2 else "avatar_claim")
                if damage == "claim_hash"
                else "avatar_reservation_fence"
            )
            audit = session.scalar(
                sa.select(Audit).where(
                    Audit.action == action,
                    Audit.correlation_id
                    == (
                        target["file_id"]
                        if action == "avatar_reservation_fence"
                        else str(s.intent_id)
                        if action == "avatar_retry_claim"
                        else json.loads(row(s)["desired_json"])["correlation_id"]
                    ),
                )
            )
            if damage == "fence_missing":
                session.delete(audit)
            elif damage == "fence_duplicate":
                values = {column.name: getattr(audit, column.name) for column in Audit.__table__.columns}
                values["id"] = str(uuid4())
                session.add(Audit(**values))
            elif damage == "fence_oversize":
                audit.summary_json = "x" * 8193
            else:
                audit.result_code = "corrupt"

    after_store(s, corrupt)
    run(s)
    assert_retained(s)


@pytest.mark.parametrize(
    "action,stage",
    [("avatar_cleanup", "reserved"), ("avatar_cleanup_complete", "cleanup_pending")],
)
def test_actual_cleanup_audit_failure_rolls_back_complete_sql_root(native, action, stage):
    s = native
    attachment_fault(s)
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER cleanup_fault AFTER INSERT ON casdoor_audit_extend "
                f"WHEN NEW.action='{action}' BEGIN SELECT RAISE(ABORT, 'synthetic cleanup fault'); END"
            )
        )
    run(s)
    with s.maker() as reader:
        assert reader.scalar(sa.select(Guard.stage)) == stage
        assert reader.scalar(sa.select(Audit.id).where(Audit.action == action)) is None
    if stage == "reserved":
        assert_retained(s)
    else:
        assert s.native.exists(reservation(s)["storage_key"]) is False
        assert json.loads(row(s)["desired_json"])["cleanup_state"] == "pending"
        assert run(s).code == "unknown"
        assert json.loads(row(s)["desired_json"])["cleanup_state"] == "pending"


@pytest.mark.parametrize(
    "damage",
    [
        "cleanup_missing",
        "cleanup_duplicate",
        "completion_missing",
        "completion_duplicate",
        "before_digest",
        "terminal_digest",
        "before_mutable",
        "guard_version",
    ],
)
def test_actual_completed_cleanup_corruption_refuses_original_closed_root(native, damage):
    from services.casdoor_avatar_consumer_service_extend import _Invocation

    s = native
    attachment_fault(s)
    run(s)
    assert json.loads(row(s)["desired_json"])["cleanup_state"] == "complete"
    target = reservation(s)
    with s.maker() as session, session.begin():
        if damage == "guard_version":
            session.execute(sa.update(Guard).where(Guard.file_id == target["file_id"]).values(version=99))
        else:
            action = "avatar_cleanup_complete" if damage.startswith("completion") else "avatar_cleanup"
            audit = session.scalar(sa.select(Audit).where(Audit.action == action))
            if damage.endswith("missing"):
                session.delete(audit)
            elif damage.endswith("duplicate"):
                values = {column.name: getattr(audit, column.name) for column in Audit.__table__.columns}
                values["id"] = str(uuid4())
                session.add(Audit(**values))
            else:
                data = json.loads(audit.summary_json)
                if damage == "before_mutable":
                    data["before_mutable"][-1][1] = ["null"]
                else:
                    data["before_sha256" if damage == "before_digest" else "terminal_sha256"] = "0" * 64
                audit.summary_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    actual = s.consumer._root(_Invocation(), lambda repo: repo._worker_root(s.intent_id))
    assert actual.failed and actual.clean and not actual.commit_attempted
    assert s.native.exists(target["storage_key"]) is False
