"""Offline sequential storage and DDL checks, not live migration/race proof."""

import ast
import copy
import inspect
import json
import runpy
import socket
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid1, uuid4

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
    InvitationAuthorityValidationError,
)

ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "api/migrations_extend/versions/2026_10_03_0001-023_invitation_authority.py"
REPOSITORY = ROOT / "api/repositories/invitation_authority_repository_extend.py"
MODELS = (InvitationAuthorityLifecycleExtend, InvitationAuthorityIssuanceExtend)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempts = []

    def deny(*_args, **_kwargs):
        attempts.append(1)
        raise AssertionError("offline network denied")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert attempts == []


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    for model in MODELS:
        model.__table__.create(engine)
    with Session(engine) as session:
        yield InvitationAuthorityRepository(), session, engine
    engine.dispose()


def payload_for(lifecycle, *, join_id=None):
    return {
        "account_id": lifecycle.account_id,
        "email": "invited@example.test",
        "workspace_id": lifecycle.workspace_id,
        "role": "normal",
        "requires_setup": True,
        "invitation_authority": {
            "schema_version": 1,
            "issuance_id": str(uuid4()),
            "lifecycle_id": lifecycle.lifecycle_id,
            "lifecycle_epoch": lifecycle.epoch,
            "token_digest": sha256(uuid4().bytes).hexdigest(),
            "join_id_at_issue": join_id,
        },
    }


def activate(storage):
    repository, session, _ = storage
    return repository.set_lifecycle_state(session, account_id=str(uuid4()), workspace_id=str(uuid4()), state="active")


def issue(storage, *, join_id=None, raw_factory=canonical):
    repository, session, _ = storage
    lifecycle = activate(storage)
    payload = payload_for(lifecycle, join_id=join_id)
    raw = raw_factory(payload)
    record = repository.record_issuance(session, payload_json=raw)
    return lifecycle, payload, record


def receipt_for(payload, record):
    authority = payload["invitation_authority"]
    return {
        **authority,
        "status": "consumed",
        "operation_id": str(uuid4()),
        "account_id": payload["account_id"],
        "workspace_id": payload["workspace_id"],
        "payload_digest": record.payload_digest,
        "key_digest": "e" * 64,
    }


def test_lifecycle_identity_idempotence_tombstones_and_regrant(storage):
    repository, session, _ = storage
    first = activate(storage)
    assert UUID(first.lifecycle_id).version == 4
    assert first.epoch == 1
    same = repository.set_lifecycle_state(
        session, account_id=first.account_id, workspace_id=first.workspace_id, state="active"
    )
    assert same == first
    withdrawn = repository.set_lifecycle_state(
        session, account_id=first.account_id, workspace_id=first.workspace_id, state="withdrawn"
    )
    assert withdrawn.lifecycle_id == first.lifecycle_id
    assert withdrawn.epoch == 2
    assert (
        repository.set_lifecycle_state(
            session, account_id=first.account_id, workspace_id=first.workspace_id, state="withdrawn"
        )
        == withdrawn
    )
    regrant = repository.set_lifecycle_state(
        session, account_id=first.account_id, workspace_id=first.workspace_id, state="active"
    )
    assert regrant.lifecycle_id == first.lifecycle_id
    assert regrant.epoch == 3
    assert regrant.created_at == first.created_at
    assert session.scalar(sa.select(sa.func.count()).select_from(MODELS[0])) == 1
    assert repository.get_lifecycle(session, account_id=first.account_id, workspace_id=first.workspace_id) == regrant


def test_absent_withdrawal_is_a_durable_tombstone_without_parent_rows(storage):
    repository, session, engine = storage
    account, workspace = str(uuid4()), str(uuid4())
    tombstone = repository.set_lifecycle_state(session, account_id=account, workspace_id=workspace, state="withdrawn")
    session.commit()
    with Session(engine) as other:
        assert repository.get_lifecycle(other, account_id=account, workspace_id=workspace) == tombstone
    assert set(sa.inspect(engine).get_table_names()) == {model.__tablename__ for model in MODELS}


def test_epoch_boundary_same_state_and_exhaustion(storage):
    repository, session, _ = storage
    lifecycle = activate(storage)
    session.execute(sa.update(MODELS[0]).values(epoch=MAX_LIFECYCLE_EPOCH))
    maximum = repository.set_lifecycle_state(
        session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="active"
    )
    assert maximum.epoch == MAX_LIFECYCLE_EPOCH
    with pytest.raises(InvitationAuthorityConflict, match="epoch_exhausted"):
        repository.set_lifecycle_state(
            session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
        )
    assert (
        repository.get_lifecycle(session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id)
        == maximum
    )


def test_owner_scope_and_missing_reads(storage):
    repository, session, _ = storage
    first = activate(storage)
    second = repository.set_lifecycle_state(
        session, account_id=first.account_id, workspace_id=str(uuid4()), state="withdrawn"
    )
    assert first.lifecycle_id != second.lifecycle_id
    assert repository.get_lifecycle(session, account_id=str(uuid4()), workspace_id=first.workspace_id) is None
    assert repository.get_issuance(session, issuance_id=str(uuid4())) is None


@pytest.mark.parametrize("join_id", [None, str(uuid4()), str(uuid1())])
def test_full_raw_snapshot_is_immutable_and_join_absence_is_explicit(storage, join_id):
    repository, session, engine = storage
    _, payload, record = issue(storage, join_id=join_id, raw_factory=lambda value: json.dumps(value, indent=2))
    assert record.payload_json == json.dumps(payload, indent=2)
    assert record.payload_digest == sha256(record.payload_json.encode()).hexdigest()
    assert record.payload_digest != sha256(canonical(payload).encode()).hexdigest()
    payload["role"] = "editor"
    with pytest.raises(FrozenInstanceError):
        record.payload_json = "changed"
    row = session.get(MODELS[1], record.issuance_id)
    assert row.role == "normal"
    assert row.join_id_at_issue == join_id
    assert "key_digest" not in row.__table__.columns
    session.commit()
    with Session(engine) as other:
        assert repository.get_issuance(other, issuance_id=record.issuance_id) == record
    assert "example.test" not in repr(record)


def test_optional_actor_and_general_uuid_account_identifiers(storage):
    repository, session, _ = storage
    lifecycle = repository.set_lifecycle_state(
        session, account_id=str(uuid1()), workspace_id=str(uuid1()), state="active"
    )
    actor = str(uuid1())
    record = repository.record_issuance(session, payload_json=canonical(payload_for(lifecycle)), actor_id=actor)
    assert record.actor_id == actor


@pytest.mark.parametrize("phase", ["lifecycle", "issuance", "consumption", "withdrawal"])
def test_caller_rollback_owns_every_write(storage, phase):
    repository, session, engine = storage
    lifecycle = activate(storage)
    if phase == "lifecycle":
        session.rollback()
        with Session(engine) as other:
            assert (
                repository.get_lifecycle(other, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id)
                is None
            )
        return
    session.commit()
    payload = payload_for(lifecycle)
    record = repository.record_issuance(session, payload_json=canonical(payload))
    if phase == "issuance":
        session.rollback()
        assert repository.get_issuance(session, issuance_id=record.issuance_id) is None
        return
    session.commit()
    if phase == "consumption":
        repository.record_consumption(session, receipt_json=canonical(receipt_for(payload, record)))
    else:
        repository.set_lifecycle_state(
            session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
        )
    session.rollback()
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == record
    assert (
        repository.get_lifecycle(session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id)
        == lifecycle
    )


def test_repository_does_not_own_transactions(storage, monkeypatch):
    repository, session, _ = storage
    session.begin()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("caller owns transaction")

    for name in ("begin", "begin_nested", "commit", "rollback"):
        monkeypatch.setattr(session, name, forbidden)
    lifecycle, payload, record = issue(storage)
    consumed = repository.record_consumption(session, receipt_json=canonical(receipt_for(payload, record)))
    assert consumed.state == "consumed"
    assert session.in_transaction()
    repository.set_lifecycle_state(
        session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
    )


@pytest.mark.parametrize("which", ["issuance_id", "token_digest"])
def test_uniqueness_conflicts_propagate_to_caller(storage, which):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    session.commit()
    duplicate = copy.deepcopy(payload)
    if which == "token_digest":
        duplicate["invitation_authority"]["issuance_id"] = str(uuid4())
    with pytest.raises(IntegrityError):
        repository.record_issuance(session, payload_json=canonical(duplicate))
    assert not session.is_active
    session.rollback()
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == record


def test_lifecycle_unique_identity(storage):
    _, session, _ = storage
    lifecycle = activate(storage)
    session.commit()
    session.add(
        MODELS[0](
            lifecycle_id=str(uuid4()),
            account_id=lifecycle.account_id,
            workspace_id=lifecycle.workspace_id,
            epoch=1,
            state="active",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize("state", ["withdrawn", "regranted", "missing", "wrong_uuid", "wrong_epoch"])
def test_issuance_requires_exact_active_lifecycle(storage, state):
    repository, session, _ = storage
    lifecycle = activate(storage)
    payload = payload_for(lifecycle)
    if state in ("withdrawn", "regranted"):
        repository.set_lifecycle_state(
            session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
        )
        if state == "regranted":
            repository.set_lifecycle_state(
                session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="active"
            )
    elif state == "missing":
        payload["workspace_id"] = str(uuid4())
    elif state == "wrong_uuid":
        payload["invitation_authority"]["lifecycle_id"] = str(uuid4())
    else:
        payload["invitation_authority"]["lifecycle_epoch"] = 2
    with pytest.raises(InvitationAuthorityConflict, match="lifecycle_conflict"):
        repository.record_issuance(session, payload_json=canonical(payload))
    assert session.scalar(sa.select(sa.func.count()).select_from(MODELS[1])) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("issuance_id", str(uuid1())),
        ("lifecycle_id", str(uuid1())),
        ("lifecycle_epoch", True),
        ("lifecycle_epoch", 0),
        ("lifecycle_epoch", MAX_LIFECYCLE_EPOCH + 1),
        ("lifecycle_epoch", 1.0),
        ("token_digest", "A" * 64),
        ("token_digest", "g" * 64),
        ("token_digest", "0" * 63),
        ("join_id_at_issue", ""),
        ("join_id_at_issue", False),
    ],
)
def test_invalid_authority_rejected_before_write(storage, field, value):
    repository, session, _ = storage
    payload = payload_for(activate(storage))
    payload["invitation_authority"][field] = value
    with pytest.raises(InvitationAuthorityValidationError):
        repository.record_issuance(session, payload_json=canonical(payload))
    assert session.scalar(sa.select(sa.func.count()).select_from(MODELS[1])) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "bad"),
        ("workspace_id", str(uuid4()).upper()),
        ("email", "bad"),
        ("email", "a@example.test\n"),
        ("email", None),
        ("email", "x" * 256),
        ("role", ""),
        ("role", "  "),
        ("role", "x" * 256),
        ("role", "\ud800"),
        ("requires_setup", 1),
        ("requires_setup", "false"),
        ("invitation_authority", []),
    ],
)
def test_invalid_payload_rejected_with_fixed_error(storage, field, value):
    repository, session, _ = storage
    payload = payload_for(activate(storage))
    payload[field] = value
    with pytest.raises(InvitationAuthorityValidationError) as raised:
        repository.record_issuance(session, payload_json=canonical(payload))
    assert str(raised.value) == "invalid_invitation_authority_facts"
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "authority_extra",
        "authority_missing",
        "duplicate",
        "invalid_json",
        "not_object",
        "nonfinite",
        "oversize",
        "surrogate",
    ],
)
def test_strict_json_snapshot_shape(storage, mutation):
    repository, session, _ = storage
    payload = payload_for(activate(storage))
    if mutation == "extra":
        payload["token"] = "forbidden"
    elif mutation == "missing":
        del payload["email"]
    elif mutation == "authority_extra":
        payload["invitation_authority"]["extra"] = "forbidden"
    elif mutation == "authority_missing":
        del payload["invitation_authority"]["join_id_at_issue"]
    raw = canonical(payload)
    raw = {
        "duplicate": raw[:-1] + ',"email":"other@example.test"}',
        "invalid_json": "{",
        "not_object": "[]",
        "nonfinite": '{"value":NaN}',
        "oversize": " " * 8193 + raw,
        "surrogate": "\ud800",
    }.get(mutation, raw)
    with pytest.raises(InvitationAuthorityValidationError):
        repository.record_issuance(session, payload_json=raw)


@pytest.mark.parametrize("actor", ["bad", 1, str(uuid4()).upper()])
def test_invalid_actor(storage, actor):
    repository, session, _ = storage
    with pytest.raises(InvitationAuthorityValidationError):
        repository.record_issuance(session, payload_json=canonical(payload_for(activate(storage))), actor_id=actor)


def test_consumption_exact_receipt_and_replay_without_update(storage):
    repository, session, engine = storage
    _, payload, record = issue(storage, raw_factory=lambda value: json.dumps(value, indent=2))
    receipt = canonical(receipt_for(payload, record))
    first = repository.record_consumption(session, receipt_json=receipt)
    assert first.state == "consumed"
    assert first.consumption_receipt_json == receipt
    assert first.consumed_at is not None
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        assert repository.record_consumption(session, receipt_json=receipt) == first
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert "email" not in json.loads(receipt)
    session.commit()
    with Session(engine) as other:
        assert repository.record_consumption(other, receipt_json=receipt) == first


@pytest.mark.parametrize("field", ["operation_id", "key_digest"])
def test_different_receipt_never_overwrites_consumption(storage, field):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    receipt = receipt_for(payload, record)
    consumed = repository.record_consumption(session, receipt_json=canonical(receipt))
    receipt[field] = str(uuid4()) if field == "operation_id" else "f" * 64
    with pytest.raises(InvitationAuthorityConflict, match="receipt_conflict"):
        repository.record_consumption(session, receipt_json=canonical(receipt))
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == consumed


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuance_id", str(uuid4())),
        ("lifecycle_id", str(uuid4())),
        ("lifecycle_epoch", 2),
        ("account_id", str(uuid4())),
        ("workspace_id", str(uuid4())),
        ("join_id_at_issue", str(uuid4())),
        ("token_digest", "d" * 64),
        ("payload_digest", "d" * 64),
    ],
)
def test_receipt_fact_mismatch_keeps_issuance_unconsumed(storage, field, value):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    receipt = receipt_for(payload, record)
    receipt[field] = value
    with pytest.raises(InvitationAuthorityConflict):
        repository.record_consumption(session, receipt_json=canonical(receipt))
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == record


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("status", "issued"),
        ("operation_id", "bad"),
        ("operation_id", str(uuid4()).upper()),
        ("issuance_id", str(uuid1())),
        ("lifecycle_id", str(uuid1())),
        ("lifecycle_epoch", 0),
        ("lifecycle_epoch", True),
        ("lifecycle_epoch", MAX_LIFECYCLE_EPOCH + 1),
        ("token_digest", "g" * 64),
        ("payload_digest", "A" * 64),
        ("key_digest", "e" * 63),
        ("join_id_at_issue", False),
    ],
)
def test_receipt_shape_validation(storage, field, value):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    receipt = receipt_for(payload, record)
    receipt[field] = value
    with pytest.raises(InvitationAuthorityValidationError):
        repository.record_consumption(session, receipt_json=canonical(receipt))
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == record


@pytest.mark.parametrize("mutation", ["extra", "missing", "whitespace", "duplicate"])
def test_receipt_requires_canonical_exact_twelve_keys(storage, mutation):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    receipt = receipt_for(payload, record)
    if mutation == "extra":
        receipt["email"] = payload["email"]
    elif mutation == "missing":
        del receipt["key_digest"]
    raw = canonical(receipt)
    if mutation == "whitespace":
        raw = " " + raw
    elif mutation == "duplicate":
        raw = raw[:-1] + ',"status":"consumed"}'
    with pytest.raises(InvitationAuthorityValidationError):
        repository.record_consumption(session, receipt_json=raw)


@pytest.mark.parametrize("regrant", [False, True])
@pytest.mark.parametrize("consumed", [False, True])
def test_withdrawal_and_regrant_fence_old_receipts(storage, regrant, consumed):
    repository, session, _ = storage
    lifecycle, payload, record = issue(storage)
    receipt = canonical(receipt_for(payload, record))
    if consumed:
        record = repository.record_consumption(session, receipt_json=receipt)
    repository.set_lifecycle_state(
        session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="withdrawn"
    )
    if regrant:
        repository.set_lifecycle_state(
            session, account_id=lifecycle.account_id, workspace_id=lifecycle.workspace_id, state="active"
        )
    with pytest.raises(InvitationAuthorityConflict, match="lifecycle_conflict"):
        repository.record_consumption(session, receipt_json=receipt)
    assert repository.get_issuance(session, issuance_id=record.issuance_id) == record


@pytest.mark.parametrize(
    "column,value",
    [
        ("payload_json", "raw_spacing"),
        ("payload_digest", "f" * 64),
        ("email", "other@example.test"),
        ("role", "editor"),
        ("requires_setup", False),
        ("join_id_at_issue", str(uuid4())),
    ],
)
def test_reconciliation_recomputes_raw_digest_and_checks_full_columns(storage, column, value):
    repository, session, _ = storage
    _, payload, record = issue(storage)
    if value == "raw_spacing":
        value = " " + record.payload_json
    session.execute(sa.update(MODELS[1]).values(**{column: value}))
    with pytest.raises(InvitationAuthorityConflict, match="payload_conflict"):
        repository.record_consumption(session, receipt_json=canonical(receipt_for(payload, record)))
    assert repository.get_issuance(session, issuance_id=record.issuance_id).state == "issued"


@pytest.mark.parametrize(
    "model,column,value",
    [
        (0, "epoch", 0),
        (0, "epoch", MAX_LIFECYCLE_EPOCH + 1),
        (0, "state", "deleted"),
        (1, "lifecycle_epoch", 0),
        (1, "state", "unknown"),
        (1, "state", "consumed"),
        (1, "token_digest", "A" * 64),
        (1, "payload_digest", "a" * 63),
    ],
)
def test_named_database_constraints_enforced_by_sqlite(storage, model, column, value):
    _, session, _ = storage
    issue(storage)
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(sa.update(MODELS[model]).values(**{column: value}))
    session.rollback()


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect()])
def test_portable_ddl_registration_and_lock_compilation(dialect):
    for model in MODELS:
        assert getattr(models, model.__name__) is model
        assert model.__name__ in models.__all__
        table = model.__table__
        assert not table.foreign_keys
        assert all(constraint.name for constraint in table.constraints)
        assert all(len(constraint.name) <= 64 for constraint in table.constraints)
        assert "FOREIGN KEY" not in str(sa.schema.CreateTable(table).compile(dialect=dialect))
        assert "FOR UPDATE" in str(sa.select(model).with_for_update().compile(dialect=dialect))
        assert not {"token", "plaintext_token", "raw_token", "redis_key"} & set(table.columns.keys())


def test_migration_head_link_and_schema_match_without_executing_migration(monkeypatch):
    migration = runpy.run_path(str(MIGRATION))
    assert migration["revision"] == "023_invitation_authority"
    assert migration["down_revision"] == "022_casdoor_avatar_cursor"
    scripts = ScriptDirectory(str(ROOT / "api/migrations_extend"))
    assert scripts.get_heads() == [migration["revision"]]
    metadata = sa.MetaData()
    drops = []
    fake_op = SimpleNamespace(
        f=sa.schema.conv,
        create_table=lambda name, *items: sa.Table(name, metadata, *items),
        drop_table=drops.append,
    )
    monkeypatch.setitem(migration["upgrade"].__globals__, "op", fake_op)
    migration["upgrade"]()
    assert set(metadata.tables) == {model.__tablename__ for model in MODELS}
    for model in MODELS:
        table = model.__table__
        migrated = metadata.tables[table.name]
        assert [(col.name, col.nullable) for col in table.columns] == [
            (col.name, col.nullable) for col in migrated.columns
        ]
        assert {constraint.name for constraint in table.constraints} == {
            constraint.name for constraint in migrated.constraints
        }
        for dialect in (postgresql.dialect(), mysql.dialect()):
            assert str(sa.schema.CreateTable(table).compile(dialect=dialect)) == str(
                sa.schema.CreateTable(migrated).compile(dialect=dialect)
            )
    migration["downgrade"]()
    assert drops == [MODELS[1].__tablename__, MODELS[0].__tablename__]


def test_repository_public_operations_require_session_and_have_no_forbidden_effects():
    for name in ("get_lifecycle", "set_lifecycle_state", "get_issuance", "record_issuance", "record_consumption"):
        parameters = inspect.signature(getattr(InvitationAuthorityRepository, name)).parameters
        assert parameters["session"].default is inspect.Parameter.empty
    tree = ast.parse(REPOSITORY.read_text())
    forbidden = {"begin", "begin_nested", "commit", "rollback", "delete", "debug", "info", "warning", "error"}
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden
    ]


def test_registration_preserves_exact_dirty_owner_baseline():
    registration = (ROOT / "api/models/__init__.py").read_text()
    registration = (
        registration.replace(
            "from .invitation_authority_extend import "
            "InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend\n",
            "",
        )
        .replace('    "InvitationAuthorityIssuanceExtend",\n', "")
        .replace('    "InvitationAuthorityLifecycleExtend",\n', "")
    )
    assert (
        sha256(registration.encode()).hexdigest() == "f745c1836d42032f86a273ad128d911d5aea2dcc264c330ad67295ac9529ab7f"
    )
