"""Independent offline checks for invitation authority's SQL fact boundary."""

import json
import runpy
import socket
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import Session

import models
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)

ROOT = Path(__file__).resolve().parents[4]
MIGRATION_FILE = ROOT / "api/migrations_extend/versions/2026_10_03_0001-023_invitation_authority.py"
LIFECYCLE = InvitationAuthorityLifecycleExtend
ISSUANCE = InvitationAuthorityIssuanceExtend


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    blocked = []

    def reject(*args, **kwargs):
        blocked.append(True)
        raise AssertionError("network disabled in independent repository check")

    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    yield
    assert blocked == []


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite://")
    LIFECYCLE.__table__.create(engine)
    ISSUANCE.__table__.create(engine)
    session = Session(engine, expire_on_commit=False)
    yield SimpleNamespace(engine=engine, session=session, repo=InvitationAuthorityRepository())
    session.close()
    engine.dispose()


def record_payload(lifecycle):
    return {
        "account_id": lifecycle.account_id,
        "email": "independent-check@example.test",
        "workspace_id": lifecycle.workspace_id,
        "role": "member",
        "requires_setup": False,
        "invitation_authority": {
            "schema_version": 1,
            "issuance_id": str(uuid4()),
            "lifecycle_id": lifecycle.lifecycle_id,
            "lifecycle_epoch": lifecycle.epoch,
            "token_digest": sha256(b"randomized token bytes; not persisted").hexdigest(),
            "join_id_at_issue": None,
        },
    }


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def start_lifecycle(db, *, state="active"):
    return db.repo.set_lifecycle_state(db.session, account_id=str(uuid4()), workspace_id=str(uuid4()), state=state)


def issued(db, *, raw=None):
    lifecycle = start_lifecycle(db)
    payload = record_payload(lifecycle)
    text = raw if raw is not None else json_bytes(payload)
    item = db.repo.record_issuance(db.session, payload_json=text)
    return lifecycle, payload, item


def make_receipt(payload, item, *, key_digest="a" * 64):
    authority = payload["invitation_authority"]
    value = {
        "schema_version": 1,
        "status": "consumed",
        "operation_id": str(uuid4()),
        "issuance_id": authority["issuance_id"],
        "lifecycle_id": authority["lifecycle_id"],
        "lifecycle_epoch": authority["lifecycle_epoch"],
        "account_id": payload["account_id"],
        "workspace_id": payload["workspace_id"],
        "join_id_at_issue": authority["join_id_at_issue"],
        "token_digest": authority["token_digest"],
        "payload_digest": item.payload_digest,
        "key_digest": key_digest,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_tombstone_survives_commit_and_regrant_advances_same_lifecycle(db):
    tombstone = start_lifecycle(db, state="withdrawn")
    db.session.commit()

    with Session(db.engine, expire_on_commit=False) as reader:
        preserved = db.repo.get_lifecycle(reader, account_id=tombstone.account_id, workspace_id=tombstone.workspace_id)
    assert preserved == tombstone and preserved.state == "withdrawn" and preserved.epoch == 1

    granted = db.repo.set_lifecycle_state(
        db.session, account_id=tombstone.account_id, workspace_id=tombstone.workspace_id, state="active"
    )
    same = db.repo.set_lifecycle_state(
        db.session, account_id=tombstone.account_id, workspace_id=tombstone.workspace_id, state="active"
    )
    withdrawn = db.repo.set_lifecycle_state(
        db.session, account_id=tombstone.account_id, workspace_id=tombstone.workspace_id, state="withdrawn"
    )

    assert granted.lifecycle_id == tombstone.lifecycle_id and granted.epoch == 2
    assert same == granted and withdrawn.lifecycle_id == tombstone.lifecycle_id and withdrawn.epoch == 3
    assert db.session.scalar(sa.select(sa.func.count()).select_from(LIFECYCLE)) == 1


def test_exact_issuance_snapshot_hash_and_outer_rollback(db):
    lifecycle = start_lifecycle(db)
    payload = record_payload(lifecycle)
    original_text = json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))
    item = db.repo.record_issuance(db.session, payload_json=original_text, actor_id=str(uuid4()))

    assert item.payload_json == original_text
    assert item.payload_digest == sha256(original_text.encode("utf-8")).hexdigest()
    assert item.state == "issued" and item.consumption_receipt_json is None
    stored = db.session.get(ISSUANCE, payload["invitation_authority"]["issuance_id"])
    assert stored.email == payload["email"] and stored.role == payload["role"]
    assert stored.lifecycle_id == lifecycle.lifecycle_id and stored.lifecycle_epoch == lifecycle.epoch
    assert "independent-check@example.test" not in repr(item)

    lifecycle_key = (lifecycle.account_id, lifecycle.workspace_id)
    issuance_id = item.issuance_id
    db.session.rollback()
    with Session(db.engine) as verifier:
        assert db.repo.get_lifecycle(verifier, account_id=lifecycle_key[0], workspace_id=lifecycle_key[1]) is None
        assert db.repo.get_issuance(verifier, issuance_id=issuance_id) is None


def test_receipt_commit_exact_replay_is_read_only_and_conflict_cannot_replace(db):
    _, payload, item = issued(db)
    receipt = make_receipt(payload, item)
    consumed = db.repo.record_consumption(db.session, receipt_json=receipt)
    assert consumed.state == "consumed" and consumed.consumption_receipt_json == receipt
    db.session.commit()

    observed_sql = []

    def remember(_connection, _cursor, sql, _parameters, _context, _many):
        observed_sql.append(sql.lstrip().split(None, 1)[0].upper())

    with Session(db.engine, expire_on_commit=False) as replay_session:
        sa.event.listen(db.engine, "before_cursor_execute", remember)
        try:
            replay = db.repo.record_consumption(replay_session, receipt_json=receipt)
        finally:
            sa.event.remove(db.engine, "before_cursor_execute", remember)
        assert replay == consumed and not set(observed_sql) & {"INSERT", "UPDATE", "DELETE"}
        receipt_keys = set(json.loads(receipt))
        assert "email" not in receipt_keys and "token" not in receipt_keys
        assert "randomized token bytes; not persisted" not in receipt

        changed = json.loads(receipt)
        changed["key_digest"] = "b" * 64
        changed_receipt = json.dumps(changed, sort_keys=True, separators=(",", ":"))
        with pytest.raises(InvitationAuthorityConflict):
            db.repo.record_consumption(replay_session, receipt_json=changed_receipt)
        assert db.repo.get_issuance(replay_session, issuance_id=item.issuance_id) == consumed


def test_old_receipt_is_fenced_after_withdrawal_and_regrant(db):
    lifecycle, payload, item = issued(db)
    receipt = make_receipt(payload, item)
    db.repo.set_lifecycle_state(
        db.session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
    )
    db.repo.set_lifecycle_state(
        db.session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="active"
    )

    with pytest.raises(InvitationAuthorityConflict, match="lifecycle_conflict"):
        db.repo.record_consumption(db.session, receipt_json=receipt)
    assert db.repo.get_issuance(db.session, issuance_id=item.issuance_id).state == "issued"


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect()])
def test_registered_models_compile_without_token_columns_or_foreign_keys(dialect):
    for model in (LIFECYCLE, ISSUANCE):
        assert getattr(models, model.__name__) is model
        assert model.__name__ in models.__all__
        assert not model.__table__.foreign_keys
        assert not {"token", "plaintext_token", "raw_token", "redis_key"} & set(model.__table__.columns.keys())
        ddl = str(sa.schema.CreateTable(model.__table__).compile(dialect=dialect))
        assert "FOREIGN KEY" not in ddl
        assert all(constraint.name for constraint in model.__table__.constraints)
        assert "FOR UPDATE" in str(sa.select(model).with_for_update().compile(dialect=dialect))


def test_migration_head_and_column_contract_match_metadata_without_execution(monkeypatch):
    migration = runpy.run_path(str(MIGRATION_FILE))
    assert migration["revision"] == "023_invitation_authority"
    assert migration["down_revision"] == "022_casdoor_avatar_cursor"
    assert ScriptDirectory(str(ROOT / "api/migrations_extend")).get_heads() == [migration["revision"]]

    shadow = sa.MetaData()
    removed = []
    intercept = SimpleNamespace(
        f=sa.schema.conv,
        create_table=lambda name, *items: sa.Table(name, shadow, *items),
        drop_table=removed.append,
    )
    monkeypatch.setitem(migration["upgrade"].__globals__, "op", intercept)
    migration["upgrade"]()
    for model in (LIFECYCLE, ISSUANCE):
        actual = model.__table__
        declared = shadow.tables[actual.name]
        assert [(col.name, col.nullable) for col in actual.columns] == [
            (col.name, col.nullable) for col in declared.columns
        ]
        for dialect in (postgresql.dialect(), mysql.dialect()):
            assert str(sa.schema.CreateTable(actual).compile(dialect=dialect)) == str(
                sa.schema.CreateTable(declared).compile(dialect=dialect)
            )
