"""Independent lost-ack recovery invariants for the F2 receipt reader."""

import hashlib
from dataclasses import FrozenInstanceError, fields
from types import MappingProxyType
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.invited_finalization_receipt import (
    FULL_HISTORY_FIELDS,
    InvitedLocalFinalizationReceipt,
    finalized_rows_sha256,
)
from core.casdoor.invited_finalization_receipt import (
    RECEIPT_KIND as F_ACTION,
)
from core.casdoor.invited_write_receipt import (
    RECEIPT_KIND as D_ACTION,
)
from core.casdoor.invited_write_receipt import (
    InvitedLocalWriteReceipt,
)
from core.casdoor.leases import CasdoorLeases
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invited_finalization_receipt_repository_extend import (
    InvitedFinalizationObservation,
)
from repositories.casdoor_invited_local_finalization_repository_extend import (
    _APPEND_PERMITS,
    CasdoorInvitedLocalFinalizationRepository,
)
from repositories.casdoor_invited_write_receipt_repository_extend import (
    CasdoorInvitedWriteReceiptRepository,
)
from services.casdoor_invited_finalization_receipt_service_extend import (
    CasdoorInvitedFinalizationReceiptService,
)

from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
)
from tests.unit_tests.services.test_casdoor_invited_local_finalization_service_extend import (
    all_state,
    finalize,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_case,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    persist,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer,
)

invited_scope_case = original_case
writer_case = original_writer


def _rows(case, model):
    with case.db() as session:
        return tuple(
            session.execute(
                sa.select(*model.__table__.columns).order_by(
                    *model.__table__.primary_key.columns
                )
            )
        )


def _primitive(row):
    return {
        field: getattr(row, field).value
        if hasattr(getattr(row, field), "value")
        else getattr(row, field)
        for field in FULL_HISTORY_FIELDS
    }


def test_durable_f1_lost_ack_recovers_in_fresh_read_root_without_effects(
    writer_case, monkeypatch
):
    case = writer_case(absent=True, invite_target=True)
    persist(case)
    owner_instances, producer_sessions, f1_commits, f1_appends = [], [], [], []
    original_produce = CasdoorInvitedLocalFinalizationRepository.produce
    original_append = CasdoorAuditRepository._append_invited_finalization_receipt

    def capture_producer(owner, *args, **kwargs):
        owner_instances.append(owner)
        return original_produce(owner, *args, **kwargs)

    def capture_append(owner, *args, **kwargs):
        f1_appends.append(True)
        return original_append(owner, *args, **kwargs)

    def f1_factory():
        session = case.factory()
        producer_sessions.append(session)
        commit = session.commit

        def durable_then_unknown():
            f1_commits.append(True)
            commit()
            raise RuntimeError("F1 commit acknowledgement lost")

        session.commit = durable_then_unknown
        return session

    monkeypatch.setattr(
        CasdoorInvitedLocalFinalizationRepository, "produce", capture_producer
    )
    monkeypatch.setattr(
        CasdoorAuditRepository, "_append_invited_finalization_receipt", capture_append
    )
    with pytest.raises(RuntimeError, match="F1 commit acknowledgement lost"):
        finalize(case, factory=f1_factory)

    assert f1_commits == [True] and f1_appends == [True]
    assert len(owner_instances) == 1 and owner_instances[0]._phase == "revoked"
    assert owner_instances[0]._receipt is None and not _APPEND_PERMITS
    assert len(producer_sessions) == 1 and not producer_sessions[0].in_transaction()
    audits_after_f1 = _rows(case, Audit)
    d_audit = next(row for row in audits_after_f1 if row.action == D_ACTION)
    f_audit = next(row for row in audits_after_f1 if row.action == F_ACTION)
    d_receipt = InvitedLocalWriteReceipt(d_audit.summary_json).values()
    f_receipt = InvitedLocalFinalizationReceipt(f_audit.summary_json)
    f_values = f_receipt.values()
    histories_after_f1 = _rows(case, History)
    assert f_values["finalized_ids"] == sorted(
        result["membership_id"]
        for result in d_receipt["results"]
        if result["membership_created"]
    )
    assert (
        f_values["write_receipt_sha256"]
        == hashlib.sha256(d_audit.summary_json.encode()).hexdigest()
    )
    assert f_values["before_postwrite_sha256"] == d_receipt["postwrite_sha256"]
    assert f_values["finalized_rows_sha256"] == finalized_rows_sha256(
        d_receipt["references"]["operation_id"],
        tuple(_primitive(row) for row in histories_after_f1),
    )

    durable_state = all_state(case)
    f2_sessions, steps, sql, projections = [], [], [], []

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "F2 invoked a writer, flush/commit, or lease/provider side effect"
        )

    def f2_factory():
        session = case.factory()
        f2_sessions.append(session)
        begin, rollback, close = session.begin, session.rollback, session.close
        session.commit = forbidden
        session.flush = forbidden

        def tracked_begin(*args, **kwargs):
            steps.append("begin")
            return begin(*args, **kwargs)

        def tracked_rollback():
            steps.append("rollback")
            return rollback()

        def tracked_close():
            steps.append("close")
            return close()

        session.begin = tracked_begin
        session.rollback = tracked_rollback
        session.close = tracked_close
        return session

    import repositories.casdoor_invited_finalization_receipt_repository_extend as f2_repository

    original_pending = f2_repository._pending

    def capture_pending(row, ids):
        projected = original_pending(row, ids)
        if row.id in ids:
            projections.append((row, projected))
        return projected

    monkeypatch.setattr(f2_repository, "_pending", capture_pending)
    monkeypatch.setattr(CasdoorInvitedLocalFinalizationRepository, "produce", forbidden)
    monkeypatch.setattr(CasdoorInvitedWriteReceiptRepository, "observe", forbidden)
    monkeypatch.setattr(
        CasdoorAuditRepository, "_append_invited_finalization_receipt", forbidden
    )
    monkeypatch.setattr(CasdoorLeases, "ensure_owned", forbidden)
    monkeypatch.setattr(case.redis, "eval", forbidden)
    monkeypatch.setattr(case.lease_redis, "eval", forbidden)

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
        sql.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", capture_sql)
    try:
        observation = CasdoorInvitedFinalizationReceiptService(
            session_factory=f2_factory,
            configuration_factory=configuration_factory,
        ).observe_invited_finalization(case.attempt, roles=case.roles)
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", capture_sql)

    assert type(observation) is InvitedFinalizationObservation
    assert {item.name for item in fields(observation)} == {
        "operation_id",
        "current_generation",
        "write_summary_sha256",
        "finalization_summary_sha256",
    }
    assert observation.operation_id == UUID(d_receipt["references"]["operation_id"])
    assert observation.current_generation == d_receipt["generation_after"]
    assert (
        observation.write_summary_sha256
        == hashlib.sha256(d_audit.summary_json.encode()).hexdigest()
    )
    assert (
        observation.finalization_summary_sha256
        == hashlib.sha256(f_audit.summary_json.encode()).hexdigest()
    )
    assert len(f2_sessions) == 1 and f2_sessions[0] is not producer_sessions[0]
    assert steps == ["begin", "rollback", "close"]
    assert sql and all(
        statement.lstrip().upper().startswith("SELECT") or statement == "BEGIN"
        for statement in sql
    )
    assert len(projections) >= 2 * len(f_values["finalized_ids"])
    for row, projected in projections:
        assert row.finalization is CasdoorFinalizationState.FINALIZED
        assert projected.finalization is CasdoorFinalizationState.PENDING
        assert dict(projected._mapping) == dict(
            row._mapping, finalization=CasdoorFinalizationState.PENDING
        )
        if "updated_at" in row._mapping:
            assert projected.updated_at == row.updated_at
        assert type(projected._mapping) is MappingProxyType
        with pytest.raises(FrozenInstanceError):
            projected._mapping = {}
        with pytest.raises(TypeError):
            projected._mapping["finalization"] = CasdoorFinalizationState.PENDING
    assert all_state(case) == durable_state
