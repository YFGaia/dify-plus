"""Read-only exact LOCAL controlled effect/audit facts for the v2 SQL reader."""

from uuid import UUID

from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    parse_local_withdrawal_json,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import TenantAccountJoin as Join
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorFinalizationState, CasdoorMembershipOwnership
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent

from repositories.casdoor_invitation_operation_repository_extend import (
    _canonical,
    _strict_json,
    _uuid,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict


def require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


def control_audit_interval(operation, audit, d_audit):
    """Inclusive original SQL precision; complete operation/D are reader-verified."""
    from repositories.casdoor_invited_write_receipt_repository_extend import _time

    require(
        _time(operation.created_at)
        <= _time(operation.terminated_at)
        <= _time(audit.created_at)
        <= _time(d_audit.created_at)
    )


def current_desired_fact(scope, history, join, fence):
    """No role snapshot is reconstructed: production and F prove signed reason."""
    data = _strict_json(
        history.desired_roles_json,
        {
            "schema_version",
            "backend",
            "target_role",
            "builtin_id",
            "role_ids",
            "reason",
            "fence_epoch",
        },
    )
    reason = CasdoorDecisionReason(data["reason"])
    if reason is CasdoorDecisionReason.ROLE_MAPPING:
        mapping = next(
            (m for m in scope.configuration.workspace_mappings if str(m.workspace_id) == join.tenant_id), None
        )
        require(mapping is not None and getattr(mapping, join.role.value, None) is not None)
    else:
        require(
            reason is CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
            and join.tenant_id == str(scope.configuration.default_workspace_id)
            and join.role is TenantAccountRole.NORMAL
        )
    require(
        history.desired_roles_json
        == _canonical(
            {
                "schema_version": 1,
                "backend": "local",
                "target_role": join.role.value,
                "builtin_id": join.role.value,
                "role_ids": [join.role.value],
                "reason": reason.value,
                "fence_epoch": fence,
            }
        )
    )


def control_audit(reader, value, history, *, withdrawal):
    """Original local effect audit with exact complete references and scalar epoch."""
    refs = value["references"]
    action = "local_member_withdraw" if withdrawal else "local_member_regrant"
    rows = reader._bounded_rows(Audit, Audit.correlation_id == refs["operation_id"], Audit.action == action, cap=100)
    expected = (
        {item["membership_id"] for item in value.get("withdrawals", ())}
        if withdrawal
        else {item["membership_id"] for item in value["results"] if item["membership_regranted"]}
    )
    operation = reader._bounded_rows(Intent, Intent.id == refs["operation_id"], cap=1)
    ds = reader._bounded_rows(
        Audit, Audit.correlation_id == refs["operation_id"], Audit.action == "invited_local_membership_write", cap=1
    )
    require(len(operation) == len(ds) == 1 and len(rows) == len(expected))
    seen = set()
    selected = []
    for row in rows:
        data = _strict_json(
            row.summary_json,
            {
                "schema_version",
                "references",
                "count",
                "epoch_before",
                "epoch_after",
                "generation",
                "fence_epoch",
            },
        )
        references = data["references"]
        require(
            type(references) is dict
            and set(references)
            == {
                "namespace_id",
                "revision_id",
                "identity_id",
                "account_id",
                "workspace_id",
                "membership_id",
                "old_join_id",
                "new_join_id",
            }
        )
        require(references["membership_id"] in expected and references["membership_id"] not in seen)
        seen.add(references["membership_id"])
        if references["membership_id"] != history.id:
            continue
        require(type(data["schema_version"]) is int and data["schema_version"] == 1)
        require(type(data["count"]) is int and data["count"] == 1)
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            require(references[field] == refs[field] == getattr(row, field))
        require(references["workspace_id"] == history.workspace_id)
        require(row.actor_account_id is None and row.result_code == "ok" and row.action == action)
        _uuid(row.id)
        control_audit_interval(operation[0], row, ds[0])
        for field in ("membership_id", "old_join_id"):
            _uuid(references[field])
        for field in ("epoch_before", "epoch_after", "generation", "fence_epoch"):
            require(type(data[field]) is int and 0 <= data[field] <= 2**63 - 1)
        require(data["epoch_after"] == data["epoch_before"] + 1 == history.ownership_epoch)
        require(data["generation"] == value["generation_after"] == history.desired_generation)
        require(data["fence_epoch"] == value["fence_epoch"])
        require(references["new_join_id"] is None if withdrawal else references["new_join_id"] == history.join_id)
        if not withdrawal:
            _uuid(references["new_join_id"])
            require(references["old_join_id"] != references["new_join_id"])
        selected.append(data)
    require(len(selected) == 1)
    return selected[0]


def controlled_regrant_fact(reader, scope, value, item, history, join):
    refs = value["references"]
    require(value["schema_version"] == 2 and item["membership_regranted"] is True)
    require(item["ownership_decision"] == "controlled_withdrawn" and not item["membership_created"])
    require(item["outcome"] == "noop" and not item["role_changed"] and not item["metadata_changed"])
    require(
        (
            history.namespace_id,
            history.identity_id,
            history.account_id,
            history.workspace_id,
            history.join_id,
            history.revision_id,
            history.desired_generation,
            history.ownership,
            history.finalization,
            history.tombstone,
        )
        == (
            refs["namespace_id"],
            refs["identity_id"],
            refs["account_id"],
            item["workspace_id"],
            join.id,
            refs["revision_id"],
            value["generation_after"],
            CasdoorMembershipOwnership.MANAGED,
            CasdoorFinalizationState.PENDING,
            False,
        )
    )
    require(join.invited_by is None)
    require(parse_role_baseline_json(history.baseline_json).backend is MembershipBackend.LOCAL)
    observation = MembershipObservation(
        UUID(join.tenant_id), UUID(join.account_id), UUID(join.id), join.role, MembershipBackend.LOCAL
    )
    require(
        history.last_applied_roles_json == role_baseline_json(observation)
        and history.last_applied_fingerprint == roles_fingerprint(observation)
    )
    current_desired_fact(scope, history, join, value["fence_epoch"])
    audit = control_audit(reader, value, history, withdrawal=False)
    require(reader._bounded_rows(Join, Join.id == audit["references"]["old_join_id"], cap=1) == ())


def controlled_withdrawal_fact(reader, value, item, history, scope):
    refs = value["references"]
    require(value["schema_version"] == 2)
    require(parse_role_baseline_json(history.baseline_json).backend is MembershipBackend.LOCAL)
    require(
        (
            history.namespace_id,
            history.identity_id,
            history.account_id,
            history.workspace_id,
            history.join_id,
            history.revision_id,
            history.desired_generation,
            history.ownership,
            history.ownership_epoch,
            history.finalization,
            history.tombstone,
        )
        == (
            refs["namespace_id"],
            refs["identity_id"],
            refs["account_id"],
            item["workspace_id"],
            item["removed_join_id"],
            refs["revision_id"],
            item["generation"],
            CasdoorMembershipOwnership.MANAGED,
            item["epoch_after"],
            CasdoorFinalizationState.PENDING,
            False,
        )
    )
    marker = parse_local_withdrawal_json(history.desired_roles_json)
    require(
        marker
        == {
            "schema_version": 2,
            "backend": "local",
            "operation": "controlled_withdrawal",
            "removed_join_id": item["removed_join_id"],
            "withdrawal_generation": item["generation"],
            "withdrawal_epoch": item["epoch_after"],
            "fence_epoch": item["fence_epoch"],
        }
    )
    observation = MembershipObservation(
        UUID(history.workspace_id), UUID(history.account_id), None, None, MembershipBackend.LOCAL
    )
    require(
        history.last_applied_roles_json == role_baseline_json(observation)
        and history.last_applied_fingerprint == roles_fingerprint(observation)
    )
    require(not any(join.tenant_id == history.workspace_id for join in scope.joins))
    require(reader._bounded_rows(Join, Join.id == item["removed_join_id"], cap=1) == ())
    audit = control_audit(reader, value, history, withdrawal=True)
    require(audit["references"]["old_join_id"] == item["removed_join_id"])
    require((audit["epoch_before"], audit["epoch_after"]) == (item["epoch_before"], item["epoch_after"]))
