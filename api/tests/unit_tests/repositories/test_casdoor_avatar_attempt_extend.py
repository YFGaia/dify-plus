"""DB-only worker reservation/unknown finish; original synthetic configuration fixture."""

import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_avatar_intent_extend import pending, storage_fixture
from test_casdoor_profile_repository_extend import NOW

from models.account import Account, Tenant, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository, _AvatarAttemptAudit
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarConflict
from repositories.casdoor_profile_repository_extend import _dump
from services.file_service import FileService

avatar_fixture = original_avatar
storage_fixture = storage_fixture


@pytest.fixture
def worker(avatar_fixture, monkeypatch):
    s = avatar_fixture
    with s.session.begin():
        join = TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id)
        s.session.add(join)
        s.session.flush()
        s.intent_id = pending(s).intent_id
        s.join_id = join.id
    s.io_calls = []

    def denied(*args, **kwargs):
        s.io_calls.append(True)
        raise AssertionError("Worker repository called an I/O or F seam")

    for name in ("normalize_avatar_image", "store_reserved_avatar", "insert_reserved_avatar"):
        monkeypatch.setattr(FileService, name, denied)
    from PIL import Image

    from extensions.ext_storage import storage

    for name in ("encrypt", "decrypt"):
        monkeypatch.setattr(type(s.config_owner.crypto), name, denied)
    for name in ("save", "load_stream", "load_once", "delete", "exists"):
        monkeypatch.setattr(storage, name, denied)
    monkeypatch.setattr(Image, "open", denied)
    yield s
    assert not s.io_calls


def claim(s, now=NOW):
    with s.session.begin():
        return s.avatar_owner.claim_and_reserve(s.intent_id, now=now)


def finish(s, attempt, reason="fetch_failed", now=NOW):
    with s.session.begin():
        return s.avatar_owner.finish_attempt(attempt, reason=reason, now=now)


def read(s):
    with Session(s.session.get_bind()) as reader:
        return dict(reader.execute(sa.select(*Intent.__table__.columns)).mappings().one())


def snapshot(s):
    with Session(s.session.get_bind()) as reader:
        return {
            model.__tablename__: [tuple(row) for row in reader.execute(sa.select(*model.__table__.columns))]
            for model in (Intent, Audit, Account, Identity, Tenant, TenantAccountJoin, UploadFile)
        }


def mutate(s, model, **values):
    with s.session.begin():
        s.session.execute(sa.update(model).values(**values))


def test_initial_claim_is_durable_owned_reservation_and_separate_reader(worker):
    s = worker
    before = snapshot(s)
    result = claim(s)
    row = read(s)
    assert result.code == "reserved"
    assert row["operation_state"] == "in_flight" and row["termination_state"] == "unconfirmed"
    assert row["attempt_count"] == 1 and row["attempt_id"] == str(result.attempt.attempt_id)
    assert row["lease_owner"] == result.attempt.lease_owner
    assert row["lease_owner"][:32] == UUID(s.join_id).hex and len(row["lease_owner"]) == 64
    assert row["lease_expires_at"] == (NOW + timedelta(seconds=60)).replace(tzinfo=None)
    assert result.reservation.tenant_id == s.revision.default_workspace_id
    assert result.reservation.account_id == s.account.id
    assert result.reservation.storage_key == (
        f"casdoor-avatar/{s.intent_id}/{result.attempt.attempt_id}/{result.reservation.file_id}.png"
    )
    data = json.loads(row["desired_json"])
    assert data["reservations"] == [
        dict(
            attempt_id=str(result.attempt.attempt_id),
            file_id=result.reservation.file_id,
            storage_key=result.reservation.storage_key,
            cleanup_state="none",
        )
    ]
    assert result.source.ciphertext == data["url_ciphertext"]
    assert result.source.sha256 == data["url_sha256"]
    assert data["url_ciphertext"] not in repr(result) and data["url_ciphertext"] not in repr(result.source)
    after = snapshot(s)
    for name in (
        Account.__tablename__,
        Identity.__tablename__,
        TenantAccountJoin.__tablename__,
        UploadFile.__tablename__,
    ):
        assert before[name] == after[name]
    with s.session.begin():
        audit = s.session.scalar(sa.select(Audit).where(Audit.action == "avatar_claim"))
        assert audit.result_code == "reserved"
        summary = json.loads(audit.summary_json)
        assert summary["reason"] == "claimed" and summary["count"] == 1
        assert set(summary) == {"schema_version", "references", "count", "generation", "fence_epoch", "reason"}
        assert data["url_ciphertext"] not in audit.summary_json and data["url_sha256"] not in audit.summary_json


@pytest.mark.parametrize(
    "reason",
    [
        "fetch_rejected",
        "fetch_failed",
        "fetch_cancelled",
        "fetch_unknown",
        "image_rejected",
        "storage_unknown",
        "attachment_lost",
        "lease_expired",
        "commit_unknown",
    ],
)
def test_every_reason_only_finish_is_unknown_never_retry_or_confirmation(worker, reason):
    result = claim(worker)
    assert finish(worker, result.attempt, reason).code == "unknown"
    row = read(worker)
    assert row["operation_state"] == "unknown" and row["termination_state"] == "manual_recovery"
    assert row["error_code"] == reason
    assert all(
        row[key] is None
        for key in (
            "retry_at",
            "terminated_at",
            "termination_proof_kind",
            "proof_ref",
            "resource_id",
            "acknowledged_at",
            "readback_at",
        )
    )
    data = json.loads(row["desired_json"])
    assert data["cleanup_state"] == data["reservations"][-1]["cleanup_state"] == "unknown"
    before = snapshot(worker)
    assert finish(worker, result.attempt).code == "unknown"
    assert claim(worker).code == "unknown"
    assert snapshot(worker) == before


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "unlinked",
        "missing_account",
        "join_replaced",
        "join_missing",
        "tenant_archived",
        "generation",
        "fence",
        "profile",
        "avatar",
        "revision",
    ],
)
def test_committed_claim_revocation_cannot_assert_cancellation_or_retry(worker, change):
    s = worker
    result = claim(s)
    with s.session.begin():
        if change == "disabled":
            s.session.execute(sa.update(Integration).values(enabled=False))
        elif change == "unlinked":
            s.session.execute(sa.delete(Identity))
        elif change == "missing_account":
            s.session.execute(sa.delete(Account))
        elif change in ("join_replaced", "join_missing"):
            s.session.execute(sa.delete(TenantAccountJoin))
            if change == "join_replaced":
                s.session.add(TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id))
        elif change == "tenant_archived":
            s.session.execute(sa.update(Tenant).values(status="archive"))
        elif change == "generation":
            s.session.execute(sa.update(Identity).values(sync_generation=2))
        elif change == "fence":
            s.session.execute(sa.update(Namespace).values(fence_epoch=1))
        elif change == "profile":
            s.session.execute(sa.update(Identity).values(profile_sync_json="{}"))
        elif change == "avatar":
            s.session.execute(sa.update(Account).values(avatar=str(uuid4())))
        else:
            s.session.execute(sa.update(Integration).values(active_revision_id=None))
    assert finish(s, result.attempt).code == "unknown"
    row = read(s)
    assert row["operation_state"] == "unknown" and row["termination_state"] == "manual_recovery"
    assert row["retry_at"] is None and row["terminated_at"] is None


@pytest.mark.parametrize("guard", ["no_begin", "autobegin", "nested", "new", "dirty", "deleted"])
def test_clean_explicit_transaction_guards(worker, guard):
    s = worker
    before = snapshot(s)
    if guard == "no_begin":
        with pytest.raises(CasdoorAvatarConflict):
            s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    elif guard == "autobegin":
        s.session.execute(sa.select(Intent.id))
        with pytest.raises(CasdoorAvatarConflict):
            s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
        s.session.rollback()
    else:
        with pytest.raises(CasdoorAvatarConflict), s.session.begin():
            if guard == "nested":
                with s.session.begin_nested():
                    s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
            else:
                if guard == "new":
                    s.session.add(Tenant(name="unrelated"))
                elif guard == "dirty":
                    s.account.name = "unflushed"
                else:
                    s.session.delete(s.identity)
                s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    assert snapshot(s) == before


def test_caller_exclusively_commits_or_whole_rolls_back(worker):
    s = worker
    before = snapshot(s)
    commits = []
    sa.event.listen(s.session, "after_commit", lambda _session: commits.append(True))
    with pytest.raises(RuntimeError, match="caller stops"), s.session.begin():
        assert s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW).code == "reserved"
        assert not commits
        raise RuntimeError("caller stops")
    assert not commits and snapshot(s) == before


def test_busy_expiry_does_not_renew_retry_or_delete(worker):
    s = worker
    claim(s)
    before = snapshot(s)
    assert claim(s, NOW + timedelta(seconds=59)).code == "busy"
    assert snapshot(s) == before
    assert claim(s, NOW + timedelta(seconds=60)).code == "unknown"
    row = read(s)
    assert row["attempt_count"] == 1 and row["retry_at"] is None
    assert row["error_code"] == "lease_expired" and row["termination_state"] == "manual_recovery"


@pytest.mark.parametrize(
    "change",
    [
        "enabled2",
        "enabled_float",
        "disabled",
        "generation",
        "fence",
        "correlation",
        "profile",
        "avatar",
        "tenant_archived",
        "join_missing",
        "issuer",
        "subject_digest",
        "policy",
        "default_tenant",
    ],
)
def test_initial_authority_denial_never_reserves(worker, change):
    s = worker
    with s.session.begin():
        if change == "enabled2":
            s.session.execute(sa.text("UPDATE casdoor_integration_extend SET enabled=2"))
        elif change == "enabled_float":
            s.session.execute(sa.text("UPDATE casdoor_integration_extend SET enabled=1.5"))
        elif change == "disabled":
            s.session.execute(sa.update(Integration).values(enabled=False))
        elif change == "generation":
            s.session.execute(sa.update(Identity).values(sync_generation=2))
        elif change == "fence":
            s.session.execute(sa.update(Namespace).values(fence_epoch=1))
        elif change in ("profile", "correlation"):
            data = json.loads(s.identity.profile_sync_json)
            if change == "profile":
                data = {}
            else:
                data["correlation_id"] = str(uuid4())
            s.session.execute(sa.update(Identity).values(profile_sync_json=_dump(data)))
        elif change == "avatar":
            s.session.execute(sa.update(Account).values(avatar=str(uuid4())))
        elif change == "tenant_archived":
            s.session.execute(sa.update(Tenant).values(status="archive"))
        elif change == "join_missing":
            s.session.execute(sa.delete(TenantAccountJoin))
        elif change == "issuer":
            s.session.execute(sa.update(Identity).values(issuer="https://other.example.test"))
        elif change == "subject_digest":
            s.session.execute(sa.update(Identity).values(subject_digest="a" * 64))
        elif change == "policy":
            policy = json.loads(s.revision.policy_json)
            policy["avatar_sync"] = False
            s.session.execute(sa.update(Revision).values(policy_json=json.dumps(policy)))
        else:
            s.session.execute(sa.update(Revision).values(default_workspace_id=str(uuid4())))
    before = snapshot(s)
    assert claim(s).code == "closed"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "model,field,size",
    [
        (Intent, "desired_json", 16385),
        (Identity, "profile_sync_json", 4097),
        (Identity, "last_applied_json", 4097),
        (Identity, "subject", 256),
        (Revision, "policy_json", 4097),
        (Revision, "mappings_json", 1048577),
        (Revision, "certificates_json", 262145),
        (Revision, "encrypted_secret", 92161),
        (Namespace, "expected_issuer", 8193),
    ],
)
def test_oversized_text_rejected_before_hydration(worker, model, field, size):
    s = worker
    mutate(s, model, **{field: "x" * size})
    loads = []
    unbounded = []

    def statement_guard(_conn, _cursor, statement, _parameters, _context, _many):
        projection = statement.lower().split("from", 1)[0]
        if statement.lstrip().lower().startswith("select") and f"{model.__tablename__}.{field}" in projection:
            if "length(" not in projection:
                unbounded.append(True)

    sa.event.listen(s.session.get_bind(), "before_cursor_execute", statement_guard)

    def check(_target, _context):
        loads.append(True)

    sa.event.listen(model, "load", check)
    try:
        with pytest.raises(CasdoorAvatarConflict), s.session.begin():
            s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
        assert not loads and not unbounded
    finally:
        sa.event.remove(model, "load", check)
        sa.event.remove(s.session.get_bind(), "before_cursor_execute", statement_guard)
    assert read(s)["attempt_count"] == 0


@pytest.mark.parametrize(
    "malformed",
    [
        "bool_generation",
        "bad_timestamp",
        "noncanonical_id",
        "float_count",
        "extra_key",
        "four_reservations",
        "reservation_mismatch",
        "bad_json",
        "oversized_after_append",
    ],
)
def test_malformed_intent_and_reservation_limits_whole_rollback(worker, malformed):
    s = worker
    row = read(s)
    data = json.loads(row["desired_json"])
    values = {}
    if malformed == "bad_timestamp":
        with s.session.begin():
            s.session.execute(sa.text("UPDATE casdoor_sync_intent_extend SET lease_expires_at='invalid-time'"))
        with pytest.raises(CasdoorAvatarConflict, match="^casdoor_avatar_conflict$"):
            claim(s)
        return
    if malformed == "bool_generation":
        data["generation"] = True
    elif malformed == "noncanonical_id":
        data["identity_id"] = data["identity_id"].upper()
    elif malformed == "float_count":
        values["attempt_count"] = 0.5
    elif malformed == "extra_key":
        data["extra"] = "not allowed"
    elif malformed == "bad_json":
        values["desired_json"] = "{broken"
    elif malformed == "oversized_after_append":
        # Escaped ASCII grows canonical bytes independently of the ciphertext character cap.
        data["url_ciphertext"] = "\\" * 7450
        while len(_dump(data).encode()) < 16200:
            data["url_ciphertext"] += "a"
    else:
        for _ in range(4 if malformed == "four_reservations" else 1):
            attempt, file = str(uuid4()), str(uuid4())
            data["reservations"].append(
                dict(
                    attempt_id=attempt,
                    file_id=file,
                    storage_key=f"casdoor-avatar/{s.intent_id}/{attempt}/{file}.png",
                    cleanup_state="none",
                )
            )
    values.setdefault("desired_json", _dump(data))
    mutate(s, Intent, **values)
    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict), s.session.begin():
        s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    assert snapshot(s) == before


def test_expired_initial_url_is_readonly_closed(worker):
    before = snapshot(worker)
    assert claim(worker, NOW + timedelta(seconds=300)).code == "closed"
    assert snapshot(worker) == before


@pytest.mark.parametrize("count", [1, 3])
def test_confirmed_due_retry_remains_held_even_with_budget(worker, count):
    s = worker
    claim(s)
    row = read(s)
    data = json.loads(row["desired_json"])
    while len(data["reservations"]) < count:
        attempt, file = str(uuid4()), str(uuid4())
        data["reservations"].append(
            dict(
                attempt_id=attempt,
                file_id=file,
                storage_key=f"casdoor-avatar/{s.intent_id}/{attempt}/{file}.png",
                cleanup_state="none",
            )
        )
    mutate(
        s,
        Intent,
        desired_json=_dump(data),
        attempt_id=data["reservations"][-1]["attempt_id"],
        attempt_count=count,
        operation_state="pending",
        termination_state="confirmed",
        retry_at=NOW.replace(tzinfo=None),
        terminated_at=NOW.replace(tzinfo=None),
        termination_proof_kind="pre_storage",
    )
    before = snapshot(s)
    assert claim(s).code == "not_due"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "target",
    [
        "claim_failure",
        "claim_intent",
        "claim_profile",
        "claim_pointer",
        "claim_audit",
        "finish_failure",
        "finish_intent",
        "finish_join",
    ],
)
def test_audit_failure_and_final_reread_triggers_roll_back_whole_root(worker, target):
    s = worker
    attempt = claim(s).attempt if target.startswith("finish") else None
    action = "avatar_finish" if attempt else "avatar_claim"
    if target.endswith("failure"):
        effect = "SELECT RAISE(ABORT, 'synthetic audit failure');"
    elif target.endswith("intent"):
        effect = "UPDATE casdoor_sync_intent_extend SET lease_owner='" + "a" * 64 + "';"
    elif target.endswith("profile"):
        effect = "UPDATE casdoor_identity_extend SET profile_sync_json='{}';"
    elif target.endswith("pointer"):
        effect = "UPDATE casdoor_integration_extend SET active_revision_id=NULL;"
    elif target.endswith("audit"):
        effect = "UPDATE casdoor_audit_extend SET result_code='forged' WHERE id=NEW.id;"
    else:
        effect = "DELETE FROM tenant_account_joins;"
    with s.session.begin():
        s.session.execute(
            sa.text(
                f"CREATE TRIGGER r1_fault AFTER INSERT ON casdoor_audit_extend "
                f"WHEN NEW.action='{action}' BEGIN {effect} END"
            )
        )
    before = snapshot(s)
    with pytest.raises((CasdoorAvatarConflict, sa.exc.IntegrityError, ValueError)), s.session.begin():
        if attempt:
            s.avatar_owner.finish_attempt(attempt, reason="fetch_failed", now=NOW)
        else:
            s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    assert snapshot(s) == before


@pytest.mark.parametrize("invalid", ["intent", "attempt", "lease", "reason", "expired_ref"])
def test_finish_cannot_choose_another_attempt_or_arbitrary_reason(worker, invalid):
    s = worker
    attempt = claim(s).attempt
    reason = "fetch_failed"
    if invalid == "intent":
        attempt = replace(attempt, intent_id=uuid4())
    elif invalid == "attempt":
        attempt = replace(attempt, attempt_id=uuid4())
    elif invalid == "lease":
        attempt = replace(attempt, lease_owner="f" * 64)
    elif invalid == "reason":
        reason = "provider secret"
    else:
        mutate(s, Intent, lease_owner="e" * 64)
    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict):
        finish(s, attempt, reason)
    assert snapshot(s) == before


def test_applied_replay_retains_owned_file_after_configuration_and_avatar_drift(worker):
    s = worker
    result = claim(s)
    data = json.loads(read(s)["desired_json"])
    data["result_file_id"] = result.reservation.file_id
    with s.session.begin():
        file = UploadFile(
            tenant_id=result.reservation.tenant_id,
            storage_type="local",
            created_at=NOW.replace(tzinfo=None),
            key=result.reservation.storage_key,
            name="avatar.png",
            size=8,
            extension="png",
            mime_type="image/png",
            created_by_role="account",
            created_by=s.account.id,
            used=True,
            hash="a" * 64,
            source_url="",
        )
        file.id = result.reservation.file_id
        s.session.add(file)
        s.session.execute(
            sa.update(Intent).values(
                desired_json=_dump(data), operation_state="applied", termination_state="confirmed", resource_id=file.id
            )
        )
        s.session.execute(sa.update(Integration).values(enabled=False))
        s.session.execute(sa.update(Identity).values(sync_generation=2))
        s.session.execute(sa.update(Account).values(avatar=str(uuid4())))
    before = snapshot(s)
    assert claim(s, NOW + timedelta(days=1)).result_file_id == UUID(file.id)
    assert snapshot(s) == before
    mutate(s, UploadFile, created_by=str(uuid4()))
    with pytest.raises(CasdoorAvatarConflict):
        claim(s)


@pytest.mark.parametrize("bad", ["action", "result", "count", "generation", "reason", "ref"])
def test_attempt_audit_has_closed_matrix_and_no_arbitrary_summary(worker, bad):
    s = worker
    event = _AvatarAttemptAudit(
        s.context.namespace_id,
        s.context.revision_id,
        s.context.identity_id,
        s.context.account_id,
        s.intent_id,
        s.correlation,
        uuid4(),
        uuid4(),
        1,
        0,
        1,
        "avatar_claim",
        "reserved",
        "claimed",
    )
    values = {
        "action": "avatar_attach",
        "result": "retry_scheduled",
        "count": 0,
        "generation": True,
        "reason": "raw sensitive error",
        "ref": "wrong",
    }
    event = replace(event, **{("file_id" if bad == "ref" else bad): values[bad]})
    before = snapshot(s)
    with pytest.raises(ValueError, match="casdoor_audit_invalid"), s.session.begin():
        CasdoorAuditRepository(s.session)._append_avatar_attempt(event)
    assert snapshot(s) == before


def test_actual_session_factory_claim_finish_and_fresh_audit_reader(worker, monkeypatch):
    import core.db.session_factory as factory
    from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
    from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

    s = worker
    # Restore the original global factory after exercising its real sessionmaker.
    monkeypatch.setattr(factory, "_session_maker", factory._session_maker)
    factory.configure_session_factory(s.session.get_bind())
    maker = factory.get_session_maker()

    def owner(session):
        configuration = CasdoorConfigurationRepository(session, crypto=s.config_owner.crypto, rbac_enabled=False)
        return CasdoorAvatarRepository(session, configuration_repository=configuration)

    with maker() as session, session.begin():
        assert isinstance(session, Session) and type(session) is not Session
        result = owner(session).claim_and_reserve(s.intent_id, now=NOW)
        assert result.code == "reserved"
    with maker() as session, session.begin():
        assert owner(session).finish_attempt(result.attempt, reason="fetch_failed", now=NOW).code == "unknown"
    with maker() as reader:
        row = reader.get(Intent, str(s.intent_id))
        assert row.operation_state == "unknown" and row.termination_state == "manual_recovery"
        assert row.attempt_count == 1 and row.retry_at is None and row.terminated_at is None
        audits = list(
            reader.scalars(
                sa.select(Audit).where(Audit.action.in_(["avatar_claim", "avatar_finish"])).order_by(Audit.action)
            )
        )
        assert [(row.action, row.result_code) for row in audits] == [
            ("avatar_claim", "reserved"),
            ("avatar_finish", "unknown"),
        ]


def test_fixed_revision_tenant_wins_over_current_join_or_other_tenant(worker):
    s = worker
    with s.session.begin():
        other = Tenant(name="Another workspace")
        s.session.add(other)
        s.session.flush()
        s.session.add(TenantAccountJoin(tenant_id=other.id, account_id=s.account.id, current=True))
    result = claim(s)
    assert result.reservation.tenant_id == s.revision.default_workspace_id != other.id
    assert result.attempt.lease_owner[:32] == UUID(s.join_id).hex


def test_audit_failure_rolls_back_already_flushed_caller_business_changes(worker):
    s = worker
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER r1_fault AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action='avatar_claim' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        )
    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict, match="^casdoor_avatar_conflict$"), s.session.begin():
        s.session.execute(sa.update(Account).values(name="caller earlier flushed work"))
        s.session.flush()
        s.avatar_owner.claim_and_reserve(s.intent_id, now=NOW)
    assert snapshot(s) == before
