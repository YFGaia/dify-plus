"""Independent invariants for the invited LOCAL finalization producer."""

import pytest
import sqlalchemy as sa

from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as F_ACTION
from core.casdoor.invited_write_receipt import (
    RECEIPT_KIND as D_ACTION,
    InvitedLocalWriteReceipt,
)
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from services.casdoor_invited_write_receipt_service_extend import (
    CasdoorInvitedWriteReceiptService,
)
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    configuration_factory,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    invited_scope_case as original_invited_scope_case,
    persist,
    writer_case as original_writer_case,
)
from services.casdoor_invited_local_finalization_service_extend import (
    CasdoorInvitedLocalFinalizationService,
)

invited_scope_case = original_invited_scope_case
writer_case = original_writer_case


def _rows(case, model):
    with case.db() as session:
        return tuple(
            session.execute(
                sa.select(*model.__table__.columns).order_by(
                    *model.__table__.primary_key.columns
                )
            )
        )


def test_finalization_appends_one_f_audit_without_rewriting_d_history(writer_case):
    case = writer_case(absent=True, invite_target=True)
    d_result = persist(case)
    audits_before = _rows(case, Audit)
    histories_before = _rows(case, History)
    d_audit = next(row for row in audits_before if row.action == D_ACTION)
    d_receipt = InvitedLocalWriteReceipt(d_audit.summary_json).values()
    finalized_ids = {
        row["membership_id"]
        for row in d_receipt["results"]
        if row["membership_created"]
    }
    assert finalized_ids == {
        str(row.membership_id) for row in d_result.workspaces if row.membership_created
    }
    assert finalized_ids

    observed = CasdoorInvitedLocalFinalizationService(
        session_factory=case.factory,
        configuration_factory=configuration_factory,
    ).finalize_invited_local_memberships(
        case.attempt,
        roles=case.roles,
        leases=case.leases,
        deadline=case.deadline,
    )

    audits_after = _rows(case, Audit)
    assert len(audits_after) == len(audits_before) + 1
    after_by_id = {row.id: dict(row._mapping) for row in audits_after}
    assert all(after_by_id[row.id] == dict(row._mapping) for row in audits_before)
    new_rows = [
        row for row in audits_after if row.id not in {old.id for old in audits_before}
    ]
    assert len(new_rows) == 1
    assert new_rows[0].action == F_ACTION
    assert new_rows[0].summary_json == observed.canonical_json

    histories_after = {row.id: dict(row._mapping) for row in _rows(case, History)}
    assert set(histories_after) == {row.id for row in histories_before}
    for old in histories_before:
        current = histories_after[old.id]
        expected = dict(old._mapping)
        if old.id in finalized_ids:
            expected["finalization"] = current["finalization"]
            expected["updated_at"] = current["updated_at"]
        assert current == expected

    audits_snapshot, histories_snapshot = _rows(case, Audit), _rows(case, History)
    with pytest.raises(CasdoorLoginScopeConflict, match="^authorization_pending$"):
        CasdoorInvitedWriteReceiptService(
            session_factory=case.factory,
            configuration_factory=configuration_factory,
        ).observe_invited_write_receipt(case.attempt)
    assert _rows(case, Audit) == audits_snapshot
    assert _rows(case, History) == histories_snapshot
