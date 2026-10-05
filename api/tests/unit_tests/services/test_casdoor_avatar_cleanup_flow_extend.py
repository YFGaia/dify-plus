"""Native fs and original consumer/SQL owners, with only bounded external HTTP seams."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from extensions.ext_storage import storage
from extensions.storage.opendal_storage import OpenDALStorage
from machinery.context import RequestContext
from models.account import Account
from models.casdoor_avatar_file_guard_extend import (
    CasdoorAvatarFileGuardExtend as Guard,
)
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.human_input import HumanInputFormUploadFile
from models.model import MessageFile
from models.tools import ToolFile
from models.workflow import WorkflowDraftVariableFile, WorkflowNodeExecutionOffload
from repositories.account_repository import SQLAlchemyAccountRepository
from services.account_errors import AvatarFileNotFoundError
from services.account_profile_service import AccountProfileService
from services.entities.account_entities import AccountProfileChanges
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_consumer_extend import (
    consumer as original_consumer,
)
from test_casdoor_avatar_consumer_extend import (
    row,
    run,
)
from test_casdoor_avatar_consumer_extend import (
    storage_fixture as original_storage_fixture,
)
from test_casdoor_avatar_consumer_extend import telemetry as original_telemetry

avatar_fixture = original_avatar_fixture
consumer = original_consumer
storage_fixture = original_storage_fixture
telemetry = original_telemetry


@pytest.fixture
def native(consumer, monkeypatch, tmp_path):
    s = consumer
    for model in (
        ToolFile,
        MessageFile,
        HumanInputFormUploadFile,
        WorkflowDraftVariableFile,
        WorkflowNodeExecutionOffload,
    ):
        model.__table__.create(s.session.get_bind())
    s.native_root = Path(tmp_path).resolve() / "native-fs"
    assert str(s.native_root).startswith("/private/tmp/")
    s.native = OpenDALStorage(scheme="fs", root=str(s.native_root))
    monkeypatch.setattr(storage, "storage_runner", s.native)
    return s


def attachment_fault(s):
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER attach_fault AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_attach' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END"
            )
        )


def after_store(s, action):
    original = s.consumer._monotonic
    fired = False

    def monotonic():
        nonlocal fired
        if not fired and list(s.native_root.rglob("*.png")):
            fired = True
            s.no_sql()
            action()
        return original()

    s.consumer._monotonic = monotonic


def reservation(s):
    return json.loads(row(s)["desired_json"])["reservations"][-1]


def profile(s, avatar):
    return AccountProfileService(accounts=SQLAlchemyAccountRepository(s.maker)).update(
        RequestContext("native-cleanup", None, s.account.id, s.revision.default_workspace_id),
        AccountProfileChanges(avatar=avatar),
    )


def assert_retained(s):
    s.no_sql()
    assert s.native.exists(reservation(s)["storage_key"]) is True


@pytest.mark.parametrize("kind", ["generation", "config", "manual"])
def test_genuine_drift_after_native_store_cleans_own_object_preserves_new_scope(native, kind):
    s = native
    manual = str(uuid4())

    def change():
        if kind == "manual":
            assert profile(s, manual).avatar == manual
        else:
            with s.maker() as session, session.begin():
                session.execute(
                    sa.update(Identity).values(sync_generation=2)
                    if kind == "generation"
                    else sa.update(Integration).values(enabled=False)
                )

    after_store(s, change)
    assert run(s).code == "unknown"
    assert s.native.exists(reservation(s)["storage_key"]) is False
    assert row(s)["termination_proof_kind"] == "avatar_cleanup"
    with s.maker() as reader:
        if kind == "manual":
            assert reader.get(Account, s.account.id).avatar == manual
        elif kind == "generation":
            assert reader.scalar(sa.select(Identity.sync_generation)) == 2
        else:
            assert reader.scalar(sa.select(Integration.enabled)) is False


@pytest.mark.parametrize("variant", ["canonical", "hex", "braces", "urn", "upper", "uuid_only", "hyphens"])
def test_genuine_manual_reference_always_vetoes_native_cleanup(native, variant):
    s = native
    attachment_fault(s)
    values = []

    def change():
        file_id = UUID(reservation(s)["file_id"])
        value = {
            "canonical": str(file_id),
            "hex": file_id.hex,
            "braces": "{" + str(file_id) + "}",
            "urn": file_id.urn,
            "upper": str(file_id).upper(),
            "uuid_only": "uuid:" + str(file_id),
            "hyphens": "-".join(file_id.hex),
        }[variant]
        values.append(value)
        profile(s, value)

    after_store(s, change)
    assert run(s).code == "unknown"
    assert_retained(s)
    with s.maker() as reader:
        assert reader.get(Account, s.account.id).avatar == values[0]
        assert reader.scalar(sa.select(Guard.stage)) == "reserved"


@pytest.mark.parametrize(
    "site",
    [
        "attach_ack",
        "terminal_ack",
        "terminal_close",
        "reader_close",
        "rollback",
        "late",
        "replacement",
        "signal",
    ],
)
def test_actual_ack_close_late_signal_and_provider_ambiguity_never_delete(native, monkeypatch, site):
    s = native
    if site != "attach_ack":
        attachment_fault(s)
    if site in ("attach_ack", "terminal_ack"):
        target = 5 if site == "attach_ack" else 6

        def lost(index):
            if index == target:
                raise RuntimeError("synthetic ACK loss")

        s.commit_hook = lost
    elif site in ("terminal_close", "reader_close"):
        target = 6 if site == "terminal_close" else 7

        def lost(index):
            if index == target:
                raise RuntimeError("synthetic close loss")

        s.close_hook = lost
    elif site == "rollback":
        original = s.maker.class_.rollback

        def rollback(session):
            if session.consumer_index == 5:
                raise RuntimeError("synthetic rollback loss")
            return original(session)

        monkeypatch.setattr(s.maker.class_, "rollback", rollback)
    elif site == "late":
        after_store(s, lambda: setattr(s, "offset", 60))
    elif site == "replacement":
        replacement = OpenDALStorage(scheme="fs", root=str(s.native_root.parent / "replacement"))
        after_store(s, lambda: monkeypatch.setattr(storage, "storage_runner", replacement))
    else:

        def shutdown():
            raise SystemExit("synthetic shutdown")

        after_store(s, shutdown)
    if site == "signal":
        with pytest.raises(SystemExit):
            run(s)
    else:
        result = run(s)
        assert result.code == ("applied" if site == "attach_ack" else "unknown")
    assert_retained(s)


def test_genuine_original_count2_native_cleanup_retains_all_first_lineage(native):
    from test_casdoor_avatar_retry_lineage_extend import prepare, produce

    s = native
    produce(s)
    first = row(s)
    with s.maker() as reader:
        audits = [dict(x) for x in reader.execute(sa.select(*Audit.__table__.columns)).mappings()]
    prepare(s)
    s.http()
    attachment_fault(s)
    assert run(s).code == "unknown"
    final = row(s)
    assert final["attempt_count"] == 2 and final["termination_proof_kind"] == "avatar_cleanup"
    assert json.loads(final["desired_json"])["reservations"][0] == json.loads(first["desired_json"])["reservations"][0]
    assert s.native.exists(reservation(s)["storage_key"]) is False
    with s.maker() as reader:
        for original in audits:
            assert (
                dict(
                    reader.execute(sa.select(*Audit.__table__.columns).where(Audit.id == original["id"]))
                    .mappings()
                    .one()
                )
                == original
            )


def test_real_cleanup_complete_permanent_manual_fence_and_original_self_projection(native, monkeypatch):
    from test_casdoor_self_avatar_status_extend import one

    from tests.unit_tests.controllers.console.test_casdoor_self_avatar_http_extend import (
        mounted,
        response,
    )

    s = native
    attachment_fault(s)
    run(s)
    assert row(s)["termination_proof_kind"] == "avatar_cleanup"
    with pytest.raises(AvatarFileNotFoundError):
        profile(s, reservation(s)["file_id"])
    assert one(s)["avatar_status"] == "failed_storage_cleaned"
    assert response(mounted(s, monkeypatch)).json["identities"][0]["avatar_status"] == "failed_storage_cleaned"


def test_real_manual_adoption_race_after_fresh_closed_reference_root_is_fenced(native):
    s = native
    attachment_fault(s)
    refused = []

    def race(index):
        if index == 7:
            s.no_sql()
            with pytest.raises(AvatarFileNotFoundError):
                profile(s, "uuid:" + reservation(s)["file_id"])
            refused.append(True)

    s.close_hook = race
    run(s)
    assert refused == [True]
    assert s.native.exists(reservation(s)["storage_key"]) is False
    with s.maker() as reader:
        assert reader.get(Account, s.account.id).avatar is None


def test_genuine_native_applied_attachment_remains_after_scope_drift_and_replay(native):
    s = native
    assert run(s).code == "applied"
    with s.maker() as session, session.begin():
        session.execute(sa.update(Identity).values(sync_generation=2))
        session.execute(sa.update(Integration).values(enabled=False))
    profile(s, str(uuid4()))
    assert run(s).code == "applied"
    assert_retained(s)


@pytest.mark.parametrize("site", ["ack", "close"])
def test_final_actual_completion_commit_ack_or_close_loss_never_reissues_delete(native, site):
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
    before = row(s)
    assert json.loads(before["desired_json"])["cleanup_state"] == "complete"
    assert s.native.exists(reservation(s)["storage_key"]) is False
    assert run(s).code == "unknown"
    assert row(s) == before


def test_actual_native_precommit_audit_rollback_cleans_exact_orphan(native):
    s = native
    attachment_fault(s)
    assert run(s).code == "unknown"
    value = row(s)
    reservation = json.loads(value["desired_json"])["reservations"][-1]
    s.no_sql()
    assert s.native.exists(reservation["storage_key"]) is False
    assert value["operation_state"] == "failed"
    assert value["termination_state"] == "confirmed"
    assert value["termination_proof_kind"] == "avatar_cleanup"
    assert json.loads(value["desired_json"])["cleanup_state"] == "complete"
    with Session(s.session.get_bind()) as reader:
        assert reader.get(Account, s.account.id).avatar is None
        assert reader.get(Guard, reservation["file_id"]).stage == "cleanup_complete"
        assert reader.scalar(sa.select(sa.func.count()).select_from(Audit).where(Audit.action == "avatar_cleanup")) == 1
