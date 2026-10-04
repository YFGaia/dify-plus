"""Independent binding checks for a real P3L Join and actual B3 summaries."""

import hashlib
import json
from enum import StrEnum

import pytest
import sqlalchemy as sa

from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MEMBERSHIP_FIELDS,
    POSTWRITE_DOMAIN,
    RECEIPT_KIND,
    InvitedLocalWriteReceipt,
)
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_invited_login_guard_repository_extend import _REGISTRY
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    persist as persist_actual_b3,
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as original_writer_case,
)

writer_case = original_writer_case


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _summary(item):
    return {
        "workspace_id": str(item.workspace_id),
        "ownership_decision": item.ownership_decision.value,
        "intent_barrier": item.intent_barrier.value,
        "outcome": item.outcome.value,
        "join_id": str(item.join_id) if item.join_id is not None else None,
        "membership_id": str(item.membership_id) if item.membership_id is not None else None,
        "current_role": item.current_role.value if item.current_role is not None else None,
        "membership_created": item.membership_created,
        "role_changed": item.role_changed,
        "metadata_changed": item.metadata_changed,
        "membership_regranted": item.membership_regranted,
    }


def _fields(session, model, names, order, *conditions):
    return [
        {key: value.value if isinstance(value, StrEnum) else value for key, value in row._mapping.items()}
        for row in session.execute(
            sa.select(*(getattr(model, name) for name in names)).where(*conditions).order_by(*order)
        )
    ]


@pytest.mark.parametrize("invite_target", [False, True])
def test_receipt_separates_real_p3l_join_from_exact_real_b3_results(writer_case, invite_target):
    case = writer_case(absent=False, invite_target=invite_target)
    b3_result = persist_actual_b3(case)
    assert b3_result.generation == case.attempt.expected_generation + 1
    assert not _REGISTRY

    with case.db() as session:
        rows = tuple(
            session.execute(
                sa.select(*Audit.__table__.columns).where(
                    Audit.action == RECEIPT_KIND,
                    Audit.correlation_id == str(case.pending.snapshot.operation_id),
                )
            )
        )
        assert len(rows) == 1
        audit_row = rows[0]
        receipt = InvitedLocalWriteReceipt(audit_row.summary_json)
        data = receipt.values()

        invitation_join = session.get(Join, case.completed.facts.join_id)
        assert invitation_join is not None
        assert invitation_join.tenant_id == str(case.attempt.workspace_id)
        assert invitation_join.account_id == str(case.attempt.account_id)

        # The serialized outcomes come from precisely the real B3 result vector.
        expected_results = [_summary(item) for item in b3_result.workspaces]
        assert data["results"] == expected_results
        result_workspaces = [item["workspace_id"] for item in expected_results]
        assert result_workspaces == sorted(set(result_workspaces))
        assert (str(case.attempt.workspace_id) in result_workspaces) is invite_target
        if invite_target:
            invited_outcome = next(
                item for item in expected_results if item["workspace_id"] == str(case.attempt.workspace_id)
            )
            assert invited_outcome["join_id"] == case.completed.facts.join_id
            assert invited_outcome["intent_barrier"] == "clear"
        else:
            assert all(item["workspace_id"] != str(case.attempt.workspace_id) for item in expected_results)

        refs = data["references"]
        assert refs["workspace_id"] == str(case.attempt.workspace_id)
        assert refs["invitation_join_id"] == case.completed.facts.join_id == invitation_join.id
        assert data["completion_proof_ref"] == case.completed.facts.proof_ref
        assert audit_row.action == RECEIPT_KIND
        assert audit_row.result_code == "verified"
        assert audit_row.actor_account_id is None
        assert audit_row.correlation_id == refs["operation_id"]
        assert audit_row.namespace_id == refs["namespace_id"]
        assert audit_row.revision_id == refs["revision_id"]
        assert audit_row.identity_id == refs["identity_id"]
        assert audit_row.account_id == refs["account_id"]
        assert audit_row.summary_json == _canonical(data)

        identity = _fields(
            session,
            Identity,
            IDENTITY_FIELDS,
            (Identity.id,),
            Identity.id == refs["identity_id"],
        )
        joins = _fields(
            session,
            Join,
            JOIN_FIELDS,
            (Join.tenant_id, Join.id),
            Join.account_id == refs["account_id"],
        )
        histories = _fields(
            session,
            History,
            MEMBERSHIP_FIELDS,
            (History.namespace_id, History.workspace_id, History.id),
            History.account_id == refs["account_id"],
        )
        projection = {"identity": identity[0], "joins": joins, "memberships": histories}
        envelope = {
            "domain": POSTWRITE_DOMAIN,
            "operation_id": refs["operation_id"],
            "postwrite": projection,
        }
        assert data["postwrite_sha256"] == hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()
        assert len(audit_row.summary_json.encode("utf-8")) <= 32 * 1024
