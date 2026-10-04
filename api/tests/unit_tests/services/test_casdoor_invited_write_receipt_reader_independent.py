"""Independent boundary checks for fields outside the invited-write digest."""

from dataclasses import fields
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from core.casdoor.invited_write_receipt import RECEIPT_KIND
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_invited_write_receipt_repository_extend import (
    InvitedWriteReceiptObservation,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from services.casdoor_invited_write_receipt_service_extend import (
    CasdoorInvitedWriteReceiptService,
)
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    database,
    persist,
    writer_case as original_writer_case,
)

writer_case = original_writer_case


def _reader(case):
    return CasdoorInvitedWriteReceiptService(
        session_factory=case.factory,
        configuration_factory=configuration_factory,
    )


def _receipt_row(case):
    with case.db() as session:
        return session.execute(
            sa.select(*Audit.__table__.columns).where(Audit.action == RECEIPT_KIND)
        ).one()


@pytest.mark.parametrize("field", ["id", "created_at"])
def test_valid_digest_excluded_audit_rewrite_is_only_an_observation(writer_case, field):
    case = writer_case(absent=True)
    persist(case)
    row = _receipt_row(case)
    with case.db() as session, session.begin():
        if field == "id":
            replacement = str(uuid4())
            session.execute(
                sa.update(Audit).where(Audit.id == row.id).values(id=replacement)
            )
        else:
            replacement = row.created_at + timedelta(microseconds=1)
            session.execute(
                sa.update(Audit)
                .where(Audit.id == row.id)
                .values(created_at=replacement)
            )

    stored = _receipt_row(case)
    observed = _reader(case).observe_invited_write_receipt(case.attempt)

    assert type(observed) is InvitedWriteReceiptObservation
    assert observed.receipt.canonical_json == stored.summary_json
    assert observed.operation_id == case.pending.snapshot.operation_id
    assert observed.completion_proof_ref == case.completed.facts.proof_ref
    assert observed.current_generation == 1
    assert {item.name for item in fields(observed)} == {
        "receipt",
        "operation_id",
        "completion_proof_ref",
        "current_generation",
    }
    assert not hasattr(observed, "audit_id")
    assert not hasattr(observed, "audit_created_at")


@pytest.mark.parametrize("field", ["id", "created_at", "history-baseline"])
def test_invalid_or_inconsistent_digest_excluded_values_fail_closed(writer_case, field):
    case = writer_case(absent=True)
    persist(case)
    row = _receipt_row(case)
    with case.db() as session, session.begin():
        if field == "id":
            session.execute(
                sa.update(Audit).where(Audit.id == row.id).values(id="not-a-uuid")
            )
        elif field == "created_at":
            session.execute(
                sa.update(Audit)
                .where(Audit.id == row.id)
                .values(created_at=row.created_at - timedelta(days=1))
            )
        else:
            session.execute(sa.update(History).values(baseline_json="{}"))

    before = database(case), _receipt_row(case)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$"):
        _reader(case).observe_invited_write_receipt(case.attempt)
    assert (database(case), _receipt_row(case)) == before
