"""Finite SQLite proofs of durable binding; no remote or current-eligibility grant."""

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor import avatar_termination as termination
from core.casdoor.crypto import CasdoorCrypto
from models.account import Account
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorManagedMembershipExtend as Membership
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_avatar_repository_extend import (
    _PRE_STORAGE_INTENT_FIELDS,
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
    _pre_storage_intent_digest,
)
from services.file_service import FileService
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_casdoor_avatar_attempt_extend import snapshot
from test_casdoor_avatar_consumer_extend import (
    avatar_fixture,
    row,
    run,
    storage_fixture,
)
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_avatar_pre_storage_termination_extend import (
    actual_receipt,
    failure_source,
    terminal_root,
)

avatar_fixture = avatar_fixture
storage_fixture = storage_fixture
consumer = original_consumer
FIELDS = (
    "namespace_id",
    "identity_id",
    "account_id",
    "workspace_id",
    "membership_id",
    "revision_id",
    "generation",
    "ownership_epoch",
    "fence_epoch",
    "kind",
    "resource_type",
    "resource_id",
    "scope_digest",
    "idempotency_key",
    "desired_json",
    "operation_state",
    "termination_state",
    "attempt_id",
    "attempt_count",
    "lease_owner",
    "lease_expires_at",
    "sent_at",
    "acknowledged_at",
    "readback_at",
    "terminated_at",
    "termination_proof_kind",
    "proof_ref",
    "retry_at",
    "error_code",
    "updated_at",
    "id",
    "created_at",
)
SUMMARY_FIELDS = [
    "count",
    "fence_epoch",
    "generation",
    "proof_ref",
    "reason",
    "references",
    "schema_version",
    "terminal_intent_sha256",
]


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def produce(s):
    invocation, attempt, tracker, capability = actual_receipt(s)
    outcome = terminal_root(s, invocation, attempt, capability)
    assert outcome.committed and outcome.clean and not outcome.failed
    assert termination._seal_pre_storage(tracker) is None
    return outcome.value


def repo(s, session):
    return CasdoorAvatarRepository(
        session, configuration_repository=s.consumer._configuration_service._repository(session)
    )


def recover(s):
    with Session(s.session.get_bind()) as session, session.begin():
        return repo(s, session).recover_pre_storage_failure(s.intent_id, now=s.utc)


def audit_row(s):
    with Session(s.session.get_bind()) as session:
        return dict(
            session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == "avatar_pre_storage"))
            .mappings()
            .one()
        )


def change_summary(s, transform):
    audit = audit_row(s)
    data = json.loads(audit["summary_json"])
    raw = transform(data)
    with s.session.begin():
        s.session.execute(sa.update(Audit).where(Audit.id == audit["id"]).values(summary_json=raw))


@contextmanager
def capture(s):
    queries = []

    def record(conn, clause, multiparams, params, opts):
        queries.append(clause)

    engine = s.session.get_bind()
    sa.event.listen(engine, "before_execute", record)
    try:
        yield queries
    finally:
        sa.event.remove(engine, "before_execute", record)


def sql(query):
    from sqlalchemy.dialects import sqlite

    return str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


def independent_digest(values):
    encoded = []
    for name in FIELDS:
        value = values[name]
        if value is None:
            tag = ["null"]
        elif type(value) is bool:
            tag = ["bool", value]
        elif type(value) is int:
            tag = ["int", value]
        elif type(value) is datetime:
            tag = ["datetime", value.strftime("%Y-%m-%dT%H:%M:%S.%f")]
        else:
            assert type(value) is str
            tag = ["str", value]
        encoded.append([name, tag])
    payload = json.dumps(encoded, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"dify-plus:casdoor:avatar-pre-storage:terminal-intent:v2\x00" + payload).hexdigest()


@pytest.mark.parametrize("mode", ["proxy", "error", "image"], ids=["proxy", "error", "image"])
def test_real_three_owner_failures_write_v2_and_recover_after_process_loss(consumer, monkeypatch, mode):
    s = consumer
    failure_source(s, monkeypatch, mode)
    assert run(s).code == "unknown"
    before = snapshot(s)
    audit = audit_row(s)
    data = json.loads(audit["summary_json"])
    assert set(data) == set(SUMMARY_FIELDS) and data["schema_version"] == 2
    assert data["proof_ref"] == row(s)["proof_ref"]
    assert canonical(data) == audit["summary_json"]
    assert len(audit["summary_json"].encode()) <= 2048
    assert "url" not in audit["summary_json"] and "ciphertext" not in audit["summary_json"]
    calls = list(s.calls)
    with capture(s) as queries:
        recovered = recover(s)
    assert recovered is not None
    assert data["terminal_intent_sha256"] == independent_digest(dict(recovered.intent_values))
    assert tuple(dict(recovered.intent_values)) == FIELDS == _PRE_STORAGE_INTENT_FIELDS
    with Session(s.session.get_bind()) as session, session.begin():
        assert repo(s, session).reconcile_pre_storage_failure(recovered, now=s.utc)
    assert snapshot(s) == before and s.calls == calls
    assert all(isinstance(q, sa.sql.Select) and q._for_update_arg is None for q in queries)
    audits = [q for q in queries if "casdoor_audit_extend" in str(q)]
    assert list(audits[0].selected_columns.keys()) == ["id"]
    assert audits[0]._limit_clause.value == 2
    assert "ORDER BY casdoor_audit_extend.id" in sql(audits[0])
    text_queries = [q for q in audits if "summary_json" in q.selected_columns.keys()]
    assert len(text_queries) == 1
    assert "length(CAST(casdoor_audit_extend.summary_json AS BLOB)) BETWEEN 0 AND 2048" in sql(text_queries[0])
    assert text_queries[0]._limit_clause.value == 2


def test_fresh_record_equals_original_exact_record_without_capability(consumer):
    s = consumer
    original = produce(s)
    expected = (original.intent_id, original.intent_values, original.audit_values)
    del original
    recovered = recover(s)
    assert recovered is not None
    assert (recovered.intent_id, recovered.intent_values, recovered.audit_values) == expected


@pytest.mark.parametrize("field", SUMMARY_FIELDS, ids=SUMMARY_FIELDS)
def test_each_v2_field_drift_is_unknown(consumer, field):
    s = consumer
    produce(s)

    def changed(data):
        data[field] = {
            "count": 2,
            "fence_epoch": 1,
            "generation": 100,
            "proof_ref": str(uuid4()),
            "reason": "fetch_rejected",
            "references": {},
            "schema_version": 1,
            "terminal_intent_sha256": "0" * 64,
        }[field]
        return canonical(data)

    change_summary(s, changed)
    assert recover(s) is None


@pytest.mark.parametrize(
    "field",
    ["namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "attempt_id", "file_id"],
    ids=["namespace_id", "revision_id", "identity_id", "account_id", "intent_id", "attempt_id", "file_id"],
)
def test_each_reference_rebinding_is_unknown(consumer, field):
    s = consumer
    produce(s)

    def changed(data):
        data["references"][field] = str(uuid4())
        return canonical(data)

    change_summary(s, changed)
    assert recover(s) is None


@pytest.mark.parametrize("field", FIELDS, ids=FIELDS)
def test_every_intent_scalar_drift_is_unknown(consumer, field):
    s = consumer
    original = produce(s)
    values = dict(original.intent_values)
    old = values[field]
    if field in ("kind", "operation_state", "termination_state"):
        changed = {"kind": "role_replace", "operation_state": "unknown", "termination_state": "unconfirmed"}[field]
    elif isinstance(Intent.__table__.columns[field].type, sa.DateTime):
        changed = (old or s.utc.replace(tzinfo=None)) + timedelta(microseconds=1)
    elif type(old) is int:
        changed = old + 1
    elif field.endswith("_id") or field in ("id", "proof_ref"):
        changed = str(uuid4())
    else:
        changed = (old or "") + "x"
    with s.session.begin():
        # Keep real FK constraints enabled: adversarial rebinding targets actual
        # alternate parents, never a fixture schema workaround.
        if field in ("namespace_id", "revision_id"):
            model = Namespace if field == "namespace_id" else Revision
            parent = dict(
                s.session.execute(sa.select(*model.__table__.columns).where(model.id == values[field])).mappings().one()
            )
            parent["id"] = changed
            if field == "revision_id":
                parent["revision_number"] += 1
            s.session.execute(sa.insert(model).values(**parent))
        elif field == "membership_id":
            s.session.execute(
                sa.insert(Membership).values(
                    id=changed,
                    namespace_id=values["namespace_id"],
                    identity_id=values["identity_id"],
                    account_id=values["account_id"],
                    workspace_id=str(s.revision.default_workspace_id),
                    ownership="released",
                    source="mapping",
                    revision_id=values["revision_id"],
                    last_applied_roles_json="{}",
                    desired_roles_json="{}",
                    baseline_json="{}",
                )
            )
        s.session.execute(
            sa.update(Intent)
            .where(Intent.id == str(s.intent_id))
            .values(**{"updated_at": values["updated_at"], field: changed})
        )
    assert recover(s) is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "delete-intent",
        "malformed",
        "oversize",
        "unicode-oversize",
        "duplicate-key",
        "nested-duplicate-key",
        "noncanonical",
        "unknown-key",
        "missing-key",
        "schema-bool",
        "count-bool",
        "generation-bool",
        "fence-overflow",
        "uuid-noncanonical",
        "hex-uppercase",
        "v1",
    ],
    ids=[
        "missing",
        "duplicate",
        "delete-intent",
        "malformed",
        "oversize",
        "unicode-oversize",
        "duplicate-key",
        "nested-duplicate-key",
        "noncanonical",
        "unknown-key",
        "missing-key",
        "schema-bool",
        "count-bool",
        "generation-bool",
        "fence-overflow",
        "uuid-noncanonical",
        "hex-uppercase",
        "v1",
    ],
)
def test_absent_ambiguous_or_invalid_audit_never_recovers(consumer, kind):
    s = consumer
    produce(s)
    audit = audit_row(s)
    data = json.loads(audit["summary_json"])
    with s.session.begin():
        if kind == "missing":
            s.session.execute(sa.delete(Audit).where(Audit.id == audit["id"]))
        elif kind == "duplicate":
            s.session.execute(sa.insert(Audit).values(**dict(audit, id=str(uuid4()))))
        elif kind == "delete-intent":
            s.session.execute(sa.delete(Intent).where(Intent.id == str(s.intent_id)))
        else:
            raw = canonical(data)
            if kind == "malformed":
                raw = "{"
            elif kind == "oversize":
                raw = "x" * 2049
            elif kind == "unicode-oversize":
                raw = "界" * 1000
            elif kind == "duplicate-key":
                raw = '{"count":1,' + raw[1:]
            elif kind == "nested-duplicate-key":
                raw = raw.replace('"references":{', '"references":{"file_id":"' + data["references"]["file_id"] + '",')
            elif kind == "noncanonical":
                raw = " " + raw
            else:
                if kind == "unknown-key":
                    data["extra"] = None
                elif kind == "missing-key":
                    data.pop("reason")
                elif kind == "schema-bool":
                    data["schema_version"] = True
                elif kind == "count-bool":
                    data["count"] = True
                elif kind == "generation-bool":
                    data["generation"] = True
                elif kind == "fence-overflow":
                    data["fence_epoch"] = 2**63
                elif kind == "uuid-noncanonical":
                    data["proof_ref"] = data["proof_ref"].upper()
                elif kind == "hex-uppercase":
                    data["terminal_intent_sha256"] = "A" * 64
                elif kind == "v1":
                    data["schema_version"] = 1
                    data.pop("proof_ref")
                    data.pop("terminal_intent_sha256")
                raw = canonical(data)
            s.session.execute(sa.update(Audit).where(Audit.id == audit["id"]).values(summary_json=raw))
    with capture(s) as queries:
        assert recover(s) is None
    if kind in ("missing", "duplicate", "oversize", "unicode-oversize"):
        assert not any("summary_json" in q.selected_columns.keys() for q in queries if isinstance(q, sa.sql.Select))


@pytest.mark.parametrize(
    "field",
    [
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "actor_account_id",
        "action",
        "result_code",
        "correlation_id",
    ],
    ids=[
        "namespace_id",
        "revision_id",
        "identity_id",
        "account_id",
        "actor_account_id",
        "action",
        "result_code",
        "correlation_id",
    ],
)
def test_audit_scalar_mismatch_is_unknown(consumer, field):
    s = consumer
    produce(s)
    audit = audit_row(s)
    changed = str(uuid4()) if field.endswith("_id") else "wrong"
    with s.session.begin():
        s.session.execute(sa.update(Audit).where(Audit.id == audit["id"]).values(**{field: changed}))
    assert recover(s) is None


@pytest.mark.parametrize(
    "state",
    ["no-root", "autobegin", "nested", "new", "dirty", "deleted", "connection", "select"],
    ids=["no-root", "autobegin", "nested", "new", "dirty", "deleted", "connection", "select"],
)
def test_invalid_or_used_root_returns_unknown_before_sql(consumer, state):
    s = consumer
    produce(s)
    with Session(s.session.get_bind()) as session:
        if state not in ("no-root", "autobegin"):
            session.begin()
        if state == "autobegin" or state == "select":
            session.execute(sa.select(Account.id))
        elif state == "nested":
            session.begin_nested()
        elif state == "new":
            session.add(Account(name="unrelated", email="unrelated@example.invalid"))
        elif state == "dirty":
            session.get(Account, str(s.account.id)).name = "dirty"
        elif state == "deleted":
            session.delete(session.get(Account, str(s.account.id)))
        elif state == "connection":
            session.connection()
        with capture(s) as queries:
            assert repo(s, session).recover_pre_storage_failure(s.intent_id, now=s.utc) is None
        assert queries == []


@pytest.mark.parametrize(
    "outcome",
    ["pending", "claimed", "transport-unknown", "storage-entered", "expired"],
    ids=["pending", "claimed", "transport-unknown", "storage-entered", "expired"],
)
def test_nonterminal_and_ambiguous_states_stay_unknown(consumer, outcome):
    s = consumer
    if outcome == "claimed":
        actual_receipt(s)
    elif outcome == "transport-unknown":
        s.http(mode="error")
        assert run(s).code == "unknown"
    elif outcome == "storage-entered":
        s.storage_fault = "save"
        assert run(s).code == "unknown"
    elif outcome == "expired":
        s.utc += timedelta(seconds=400)
        assert run(s).code == "unknown"
    before = snapshot(s)
    assert recover(s) is None
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["sql", "intent-reread", "audit-reread"], ids=["sql", "intent-reread", "audit-reread"]
)
def test_reader_database_error_or_late_drift_returns_unknown(consumer, monkeypatch, fault):
    s = consumer
    produce(s)
    original = CasdoorAvatarRepository.reconcile_pre_storage_failure
    initial_audit = audit_row(s)
    duplicate = False

    def fail(repo, record, *, now):
        if fault == "sql":
            raise OperationalError("synthetic read", {}, RuntimeError("offline fault"))
        if fault == "intent-reread":
            repo._session.execute(sa.update(Intent).values(proof_ref=str(uuid4())))
        elif duplicate:
            repo._session.execute(sa.insert(Audit).values(**dict(initial_audit, id=str(uuid4()))))
        else:
            repo._session.execute(
                sa.update(Audit).where(Audit.action == "avatar_pre_storage").values(summary_json="{}")
            )
        return original(repo, record, now=now)

    monkeypatch.setattr(CasdoorAvatarRepository, "reconcile_pre_storage_failure", fail)
    assert recover(s) is None

    if fault == "audit-reread":
        with s.session.begin():
            s.session.execute(
                sa.update(Audit)
                .where(Audit.id == initial_audit["id"])
                .values(summary_json=initial_audit["summary_json"])
            )
        duplicate = True
        assert recover(s) is None


@pytest.mark.parametrize(
    "fault", ["audit-insert", "audit-flush", "final-reread"], ids=["audit-insert", "audit-flush", "final-reread"]
)
def test_v2_write_fault_rolls_back_whole_caller_root(consumer, monkeypatch, fault):
    s = consumer
    invocation, attempt, tracker, capability = actual_receipt(s)
    before = snapshot(s)
    if fault == "audit-insert":
        monkeypatch.setattr(
            CasdoorAuditRepository, "_append_avatar_pre_storage", Mock(side_effect=ValueError("audit fault"))
        )
    elif fault == "final-reread":
        monkeypatch.setattr(CasdoorAvatarRepository, "reconcile_pre_storage_failure", lambda *a, **k: False)
    else:
        original = s.maker.class_.flush

        def fail_flush(session, *args, **kwargs):
            if any(type(obj) is Audit and obj.action == "avatar_pre_storage" for obj in session.new):
                raise OperationalError("synthetic flush", {}, RuntimeError("offline fault"))
            return original(session, *args, **kwargs)

        monkeypatch.setattr(s.maker.class_, "flush", fail_flush)

    def operation(owner):
        owner._session.execute(sa.update(Account).values(name="prior caller change"))
        owner._session.flush()
        return owner.confirm_pre_storage_failure(attempt, capability, now=s.utc)

    outcome = s.consumer._root(invocation, operation, write=True)
    assert outcome.failed and not outcome.commit_attempted
    assert snapshot(s) == before and termination._seal_pre_storage(tracker) is None


@pytest.mark.parametrize(
    "kind",
    ["float", "int-overflow", "aware-datetime", "subclass-str", "extra-field", "missing-field", "schema-drift"],
    ids=["float", "int-overflow", "aware-datetime", "subclass-str", "extra-field", "missing-field", "schema-drift"],
)
def test_digest_rejects_unsupported_scalar_or_schema_drift(consumer, monkeypatch, kind):
    s = consumer
    record = produce(s)
    values = dict(record.intent_values)
    if kind == "float":
        values["generation"] = 1.0
    elif kind == "int-overflow":
        values["generation"] = 2**63
    elif kind == "aware-datetime":
        values["created_at"] = datetime.now(UTC)
    elif kind == "subclass-str":

        class Other(str):
            pass

        values["error_code"] = Other("fetch_failed")
    elif kind == "extra-field":
        values["extra"] = None
    elif kind == "missing-field":
        values.pop("proof_ref")
    else:
        import repositories.casdoor_avatar_repository_extend as owner

        monkeypatch.setattr(owner, "_PRE_STORAGE_INTENT_FIELDS", FIELDS[:-1])
    with pytest.raises(CasdoorAvatarConflict):
        _pre_storage_intent_digest(values)


def test_recovery_uses_no_lifecycle_storage_or_capability_owner(consumer, monkeypatch):
    s = consumer
    produce(s)
    before = snapshot(s)
    for owner, name in [
        (FileService, "store_reserved_avatar"),
        (FileService, "insert_reserved_avatar"),
        (CasdoorCrypto, "decrypt"),
        (termination, "_seal_pre_storage"),
        (termination, "_consume_pre_storage"),
    ]:
        monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError("SQL read only")))
    calls = list(s.calls)
    with Session(s.session.get_bind()) as session, session.begin():
        with monkeypatch.context() as guard, capture(s) as queries:
            for name in ("flush", "commit", "rollback", "begin_nested"):
                guard.setattr(session, name, Mock(side_effect=AssertionError("caller owns lifecycle")))
            assert repo(s, session).recover_pre_storage_failure(s.intent_id, now=s.utc) is not None
        assert all(isinstance(q, sa.sql.Select) and q._for_update_arg is None for q in queries)
    assert snapshot(s) == before and s.calls == calls
