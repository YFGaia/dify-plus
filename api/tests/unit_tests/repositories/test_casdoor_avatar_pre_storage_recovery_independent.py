"""Independent adversarial checks for durable pre-storage recovery."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from sqlalchemy.orm import Session
from test_casdoor_avatar_pre_storage_recovery_extend import (
    audit_row,
    capture,
    produce,
    recover,
    repo,
)

pytest_plugins = ("test_casdoor_avatar_pre_storage_recovery_extend",)

INTENT_FIELDS = (
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


def independent_digest(values):
    pairs = []
    for name in INTENT_FIELDS:
        value = values[name]
        if value is None:
            tagged = ["null"]
        elif type(value) is bool:
            tagged = ["bool", value]
        elif type(value) is int and -(2**63) <= value <= 2**63 - 1:
            tagged = ["int", value]
        elif type(value) is str:
            tagged = ["str", value]
        elif type(value) is datetime and value.tzinfo is None:
            tagged = ["datetime", value.isoformat(timespec="microseconds")]
        else:
            raise AssertionError(f"unsupported independent digest value: {name}={type(value)!r}")
        pairs.append([name, tagged])
    payload = json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"dify-plus:casdoor:avatar-pre-storage:terminal-intent:v2\x00" + payload).hexdigest()


def test_late_duplicate_after_candidate_enumeration_is_unknown(consumer, monkeypatch):
    s = consumer
    produce(s)
    calls = list(s.calls)
    existing = audit_row(s)
    original = CasdoorAvatarRepository._worker_read
    inserted = []

    def insert_duplicate_before_audit_read(repository, model, where, *, fields=None, lock=True):
        if model is Audit and not inserted:
            assert lock is False
            duplicate = dict(existing, id=str(uuid4()))
            repository._session.execute(sa.insert(Audit).values(**duplicate))
            inserted.append(duplicate["id"])
        return original(repository, model, where, fields=fields, lock=lock)

    monkeypatch.setattr(CasdoorAvatarRepository, "_worker_read", insert_duplicate_before_audit_read)
    with capture(s) as queries, Session(s.session.get_bind()) as session:
        session.begin()
        assert repo(s, session).recover_pre_storage_failure(s.intent_id, now=s.utc) is None
        session.rollback()

    candidate_queries = [
        query
        for query in queries
        if isinstance(query, sa.sql.Select)
        and query.get_final_froms()[0].name == "casdoor_audit_extend"
        and list(query.selected_columns.keys()) == ["id"]
        and query._limit_clause is not None
        and query._limit_clause.value == 2
        and query._order_by_clauses
        and "correlation_id" in str(query)
        and "action" in str(query)
    ]
    assert len(inserted) == 1
    assert len(candidate_queries) == 2
    assert [query.selected_columns.keys() for query in candidate_queries] == [["id"], ["id"]]
    assert recover(s) is not None  # The test-injected duplicate was rolled back with its caller root.
    assert s.calls == calls


def test_independent_32_scalar_digest_binds_created_at_microseconds(consumer):
    s = consumer
    record = produce(s)
    calls = list(s.calls)
    values = dict(record.intent_values)
    assert tuple(values) == INTENT_FIELDS and len(INTENT_FIELDS) == 32
    audit = audit_row(s)
    summary = json.loads(audit["summary_json"])
    assert summary["terminal_intent_sha256"] == independent_digest(values)
    assert type(values["created_at"]) is datetime and values["created_at"].tzinfo is None

    with s.session.begin():
        changed = s.session.execute(
            sa.update(Intent)
            .where(Intent.id == str(s.intent_id))
            .values(created_at=values["created_at"] + timedelta(microseconds=1))
        )
        assert changed.rowcount == 1
    assert recover(s) is None
    assert s.calls == calls
