"""Offline DB attachment foundation; actual F insertion and configured session factory."""

import json
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session
from test_casdoor_avatar_intent_extend import avatar as original_avatar
from test_casdoor_avatar_intent_extend import pending, storage_fixture
from test_casdoor_profile_repository_extend import NOW

from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.model import UploadFile
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
)
from repositories.casdoor_configuration_repository_extend import (
    CasdoorConfigurationRepository,
)
from repositories.casdoor_profile_repository_extend import _dump
from services.account_avatar_file_gateway import SQLAlchemyAccountAvatarFileGateway
from services.file_service import FileService

avatar_fixture = original_avatar
storage_fixture = storage_fixture


@pytest.fixture
def attachment(avatar_fixture, monkeypatch):
    import redis

    import core.db.session_factory as factory
    from extensions.ext_storage import storage

    s = avatar_fixture
    with s.session.begin():
        join = TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id)
        s.session.add(join)
        s.session.flush()
        s.intent_id = pending(s).intent_id
    output = BytesIO()
    with Image.new("RGB", (2, 2), "red") as image:
        image.save(output, format="PNG")
    s.normalized = FileService.normalize_avatar_image(output.getvalue()).image
    assert s.normalized is not None
    monkeypatch.setattr(factory, "_session_maker", factory._session_maker)
    factory.configure_session_factory(s.session.get_bind())
    s.maker = factory.get_session_maker()
    s.io_calls = []

    def denied(*args, **kwargs):
        s.io_calls.append(True)
        raise AssertionError("DB attachment reached external I/O")

    for name in ("normalize_avatar_image", "store_reserved_avatar"):
        monkeypatch.setattr(FileService, name, denied)
    for name in ("encrypt", "decrypt"):
        monkeypatch.setattr(type(s.config_owner.crypto), name, denied)
    for name in ("save", "load_stream", "load_once", "delete", "exists"):
        monkeypatch.setattr(storage, name, denied)
    monkeypatch.setattr(Image, "open", denied)
    monkeypatch.setattr(redis.Redis, "execute_command", denied)
    with s.maker() as session, session.begin():
        assert isinstance(session, Session) and type(session) is not Session
        s.claimed = owner(s, session).claim_and_reserve(s.intent_id, now=NOW)
    assert s.claimed.code == "reserved"
    yield s
    assert not s.io_calls


def owner(s, session):
    config = CasdoorConfigurationRepository(session, crypto=s.config_owner.crypto, rbac_enabled=False)
    return CasdoorAvatarRepository(session, configuration_repository=config)


def attach(s, *, attempt=None, now=NOW, normalized=None):
    with s.maker() as session, session.begin():
        return owner(s, session).recheck_and_attach(attempt or s.claimed.attempt, normalized or s.normalized, now=now)


def reconcile(s, **kwargs):
    with s.maker() as session:
        session.begin()
        try:
            return owner(s, session).reconcile_attachment(s.claimed.attempt, now=NOW, **kwargs)
        finally:
            session.rollback()


def snapshot(s):
    with s.maker() as reader:
        return {
            model.__tablename__: [
                tuple(row)
                for row in reader.execute(
                    sa.select(
                        *(
                            sa.type_coerce(column, sa.String) if column.name == "storage_type" else column
                            for column in model.__table__.columns
                        )
                    )
                )
            ]
            for model in (Account, Intent, UploadFile, Audit)
        }


def test_actual_insert_gateway_and_readonly_replay_lost_ack(attachment, monkeypatch):
    s = attachment
    result = attach(s)
    assert result.code == "attached" and str(result.result_file_id) == s.claimed.reservation.file_id
    with s.maker() as reader:
        row = reader.get(Intent, str(s.intent_id))
        account = reader.get(Account, s.account.id)
        file = reader.get(UploadFile, str(result.result_file_id))
        assert row.operation_state == "applied" and row.termination_state == "confirmed"
        assert row.resource_id == row.proof_ref == account.avatar == file.id
        assert row.termination_proof_kind == "avatar_attachment_db"
        assert row.readback_at == row.terminated_at == account.updated_at == NOW.replace(tzinfo=None)
        assert row.lease_owner == s.claimed.attempt.lease_owner
        assert file.hash == s.normalized.sha3_256 and file.source_url == "" and file.used is True
        assert file.used_by == account.id and file.tenant_id == s.revision.default_workspace_id
        audit = reader.scalar(sa.select(Audit).where(Audit.action == "avatar_attach"))
        assert audit.result_code == "applied" and json.loads(audit.summary_json)["reason"] == "attached"
    calls = []
    monkeypatch.setattr(
        "services.account_avatar_file_gateway.file_helpers.get_signed_file_url",
        lambda **kw: calls.append(kw) or "https://signed.example/avatar",
    )
    gateway = SQLAlchemyAccountAvatarFileGateway(session_factory=s.maker)
    assert gateway.get_owned_signed_url(account_id=s.account.id, upload_file_id=str(result.result_file_id))
    assert gateway.get_owned_signed_url(account_id=str(uuid4()), upload_file_id=str(result.result_file_id)) is None
    assert len(calls) == 1
    monkeypatch.setattr(FileService, "insert_reserved_avatar", lambda *a: pytest.fail("replay inserted"))
    before = snapshot(s)
    assert attach(s).code == "replayed"
    assert (
        reconcile(s, expected_sha3_256=s.normalized.sha3_256, expected_size=len(s.normalized.content)).code == "applied"
    )
    assert snapshot(s) == before
    with s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(avatar=str(uuid4())))
        session.execute(sa.update(Integration).values(enabled=False))
        session.execute(sa.update(Identity).values(sync_generation=99))
        session.execute(sa.delete(TenantAccountJoin))
    before = snapshot(s)
    assert reconcile(s).result_file_id == result.result_file_id
    assert attach(s).code == "replayed"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "drift", ["join", "disabled", "fence", "generation", "watermark", "baseline", "unknown", "expiry", "ref"]
)
def test_denied_attachment_keeps_claim_and_rolls_back_caller(attachment, drift):
    s = attachment
    with s.maker() as session, session.begin():
        if drift == "join":
            session.execute(sa.delete(TenantAccountJoin))
            session.add(TenantAccountJoin(tenant_id=s.revision.default_workspace_id, account_id=s.account.id))
        elif drift == "disabled":
            session.execute(sa.update(Integration).values(enabled=False))
        elif drift == "fence":
            session.execute(sa.update(Namespace).values(fence_epoch=1))
        elif drift == "generation":
            session.execute(sa.update(Identity).values(sync_generation=2))
        elif drift == "watermark":
            session.execute(sa.update(Identity).values(profile_sync_json="{}"))
        elif drift == "baseline":
            session.execute(sa.update(Account).values(avatar=str(uuid4())))
        elif drift == "unknown":
            owner(s, session).finish_attempt(s.claimed.attempt, now=NOW, reason="commit_unknown")
    before = snapshot(s)
    attempt = replace(s.claimed.attempt, attempt_id=uuid4()) if drift == "ref" else s.claimed.attempt
    now = NOW + timedelta(seconds=60) if drift == "expiry" else NOW
    with pytest.raises(CasdoorAvatarConflict), s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(name="caller write"))
        session.flush()
        owner(s, session).recheck_and_attach(attempt, s.normalized, now=now)
    assert snapshot(s) == before
    assert reconcile(s).code == "unknown"


@pytest.mark.parametrize("fault", ["collision", "audit", "parent", "intent", "file", "account"])
def test_insert_and_final_read_failures_roll_back_whole_root(attachment, fault):
    s = attachment
    with s.maker() as session, session.begin():
        if fault == "collision":
            inserted = FileService.insert_reserved_avatar(session, s.claimed.reservation, s.normalized)
            assert inserted.code == "inserted"
            session.execute(sa.update(UploadFile).values(created_by=str(uuid4())))
        else:
            sql = {
                "audit": "SELECT RAISE(ABORT, 'synthetic audit fault');",
                "parent": "UPDATE casdoor_identity_extend SET sync_generation=2;",
                "intent": "UPDATE casdoor_sync_intent_extend SET proof_ref=NULL;",
                "file": "UPDATE upload_files SET hash='" + "f" * 64 + "';",
                "account": "UPDATE accounts SET updated_at='2020-01-01 00:00:00.000000';",
            }[fault]
            session.execute(
                sa.text(
                    "CREATE TRIGGER attachment_fault AFTER INSERT ON casdoor_audit_extend "
                    "WHEN NEW.action='avatar_attach' BEGIN " + sql + " END"
                )
            )
    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict), s.maker() as session, session.begin():
        session.execute(sa.update(Account).values(name="caller write"))
        session.flush()
        owner(s, session).recheck_and_attach(s.claimed.attempt, s.normalized, now=NOW)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "bad",
    ["proof", "lease", "attempt", "hash", "size", "used_float", "source", "key", "name", "storage", "owner", "missing"],
)
def test_reconciliation_rejects_malformed_owned_evidence_without_writes(attachment, bad):
    s = attachment
    attach(s)
    with s.maker() as session, session.begin():
        if bad == "proof":
            session.execute(sa.update(Intent).values(termination_proof_kind="legacy"))
        elif bad == "lease":
            session.execute(sa.update(Intent).values(lease_owner=None, lease_expires_at=None))
        elif bad == "attempt":
            session.execute(sa.update(Intent).values(attempt_id=str(uuid4())))
        elif bad == "storage":
            session.execute(sa.text("UPDATE upload_files SET storage_type='unknown'"))
        elif bad == "used_float":
            session.execute(sa.text("UPDATE upload_files SET used=1.5"))
        elif bad == "missing":
            session.execute(sa.delete(UploadFile))
        else:
            values = {
                "hash": {"hash": "X" * 64},
                "size": {"size": 0},
                "source": {"source_url": "x"},
                "key": {"key": "x" * 256},
                "name": {"name": "x" * 256},
                "storage": {"storage_type": "unknown"},
                "owner": {"created_by": str(uuid4())},
            }[bad]
            session.execute(sa.update(UploadFile).values(**values))
    before = snapshot(s)
    result = reconcile(s)
    assert result.code == "unknown" and result.result_file_id is None
    with pytest.raises(CasdoorAvatarConflict):
        attach(s)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "digest,size",
    [(None, 1), ("a" * 64, None), ("A" * 64, 1), ("a" * 64, True), ("a" * 64, 0), ("a" * 64, 2097153), ("a" * 64, 1)],
)
def test_optional_comparison_is_exact_pair_and_never_authority(attachment, digest, size):
    s = attachment
    attach(s)
    before = snapshot(s)
    assert reconcile(s, expected_sha3_256=digest, expected_size=size).code == "unknown"
    assert snapshot(s) == before


@pytest.mark.parametrize("bad_prior", ["proof", "file", "late_proof", "late_file"])
def test_managed_second_generation_requires_strict_prior_proof(attachment, bad_prior):
    s = attachment
    first = attach(s)
    with s.maker() as session, session.begin():
        previous = dict(session.execute(sa.select(*Intent.__table__.columns)).mappings().one())
        data = json.loads(previous["desired_json"])
        new_id = str(uuid4())
        # Persisted profile generation/intent fixture: no new runtime consumer claimed.
        identity = session.get(Identity, previous["identity_id"])
        sync = json.loads(identity.profile_sync_json)
        sync["generation"] = 2
        baseline = json.loads(identity.last_applied_json)
        identity.sync_generation = 2
        identity.profile_sync_json = _dump(sync)
        identity.last_applied_json = _dump(baseline)
        from repositories.casdoor_avatar_repository_extend import _key

        data.update(generation=2, baseline=str(first.result_file_id), result_file_id=None, reservations=[])
        previous.update(
            id=new_id,
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
        claimed = owner(s, session).claim_and_reserve(UUID(new_id), now=NOW)
    assert claimed.code == "reserved"
    if bad_prior.startswith("late_"):
        mutation = (
            f"UPDATE casdoor_sync_intent_extend SET proof_ref=NULL WHERE id='{s.intent_id}';"
            if bad_prior == "late_proof"
            else f"UPDATE upload_files SET hash='{'f' * 64}' WHERE id='{first.result_file_id}';"
        )
        with s.maker() as session, session.begin():
            session.execute(
                sa.text(
                    "CREATE TRIGGER prior_drift AFTER INSERT ON casdoor_audit_extend "
                    "WHEN NEW.action='avatar_attach' BEGIN " + mutation + " END"
                )
            )
        before = snapshot(s)
        with pytest.raises(CasdoorAvatarConflict), s.maker() as session, session.begin():
            session.execute(sa.update(Account).values(name="earlier caller write"))
            session.flush()
            owner(s, session).recheck_and_attach(claimed.attempt, s.normalized, now=NOW)
        assert snapshot(s) == before
        return
    with s.maker() as session, session.begin():
        if bad_prior == "proof":
            session.execute(
                sa.update(Intent).where(Intent.id == str(s.intent_id)).values(termination_proof_kind="legacy")
            )
        else:
            session.execute(sa.update(UploadFile).values(used_by=str(uuid4())))
    before = snapshot(s)
    with pytest.raises(CasdoorAvatarConflict):
        attach(s, attempt=claimed.attempt)
    assert snapshot(s) == before
    with s.maker() as session, session.begin():
        session.execute(
            sa.update(Intent)
            .where(Intent.id == str(s.intent_id))
            .values(termination_proof_kind="avatar_attachment_db", updated_at=NOW.replace(tzinfo=None))
        )
        session.execute(sa.update(UploadFile).values(used_by=s.account.id))
    assert attach(s, attempt=claimed.attempt).code == "attached"


def test_file_byte_bounds_precede_text_hydration(attachment):
    s = attachment
    attach(s)
    with s.maker() as session, session.begin():
        session.execute(sa.update(UploadFile).values(name="中" * 86))
    queries = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "upload_files.name" in statement:
            queries.append(statement)

    engine = s.session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        assert reconcile(s).code == "unknown"
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
    assert queries
    assert all("length(CAST(upload_files.name AS BLOB))" in query for query in queries)
    assert all("upload_files.created_by =" in query and "upload_files.used_by =" in query for query in queries)
