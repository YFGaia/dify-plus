"""Full-row recovery deltas tied to original D/F and LOCAL control audit facts.

The caller already performed the original current SQL reader and admission.
These immutable comparisons issue no continuation, guard or write result.
"""

from datetime import UTC, datetime

from core.casdoor.invited_controlled_write_receipt import controlled_finalization_ids
from core.casdoor.invited_finalization_receipt import InvitedLocalFinalizationReceipt
from core.casdoor.invited_write_receipt import InvitedLocalWriteReceipt
from models.casdoor_extend import CasdoorFinalizationState

from repositories.casdoor_invitation_operation_repository_extend import _strict_json
from repositories.casdoor_invited_controlled_history_repository_extend import (
    control_audit_interval,
)
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict


def require(value):
    if not value:
        raise CasdoorLoginScopeConflict()


def same(prior, current, mutable=()):
    require(set(prior._mapping) == set(current._mapping))
    require(all(getattr(current, key) == value for key, value in prior._mapping.items() if key not in mutable))


def monotonic(prior, current):
    require(type(current.updated_at) is datetime and current.updated_at.tzinfo is None)
    require(type(prior.updated_at) is datetime and prior.updated_at.tzinfo is None)
    require(prior.updated_at <= current.updated_at <= datetime.now(UTC).replace(tzinfo=None))


def verify_recovery_membership_delta(before, after, *, finalizing):
    old, rows = before
    new, fresh = after
    operation = str(old.snapshot.operation_id)
    previous_audits = {row.id: row for row in rows["audits"]}
    current_audits = {row.id: row for row in fresh["audits"]}
    require(len(previous_audits) == len(rows["audits"]) and len(current_audits) == len(fresh["audits"]))
    require(all(current_audits.get(key) == row for key, row in previous_audits.items()))
    added = tuple(row for key, row in current_audits.items() if key not in previous_audits)
    ds = tuple(
        row
        for row in fresh["audits"]
        if row.correlation_id == operation and row.action == "invited_local_membership_write"
    )
    require(len(ds) == 1)
    d = InvitedLocalWriteReceipt(ds[0].summary_json).values()
    refs = d["references"]
    require(refs["operation_id"] == operation and refs["account_id"] == str(new.attempt.account_id))
    for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
        require(getattr(ds[0], field) == refs[field])
    require(ds[0].actor_account_id is None and ds[0].result_code == "verified")
    old_histories = {row.id: row for row in rows["histories"]}
    histories = {row.id: row for row in fresh["histories"]}
    if finalizing:
        require(len(added) == 1 and added[0].action == "invited_local_membership_finalization")
        f = InvitedLocalFinalizationReceipt(added[0].summary_json).values()
        require(added[0].correlation_id == operation and f["references"] == refs)
        require(added[0].actor_account_id is None and added[0].result_code == "verified")
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            require(getattr(added[0], field) == refs[field])
        ids = set(controlled_finalization_ids(d))
        require(f["finalized_ids"] == sorted(ids) and set(old_histories) == set(histories))
        for key, prior in old_histories.items():
            row = histories[key]
            if key in ids:
                same(prior, row, ("finalization", "updated_at"))
                require(
                    prior.finalization is CasdoorFinalizationState.PENDING
                    and row.finalization is CasdoorFinalizationState.FINALIZED
                )
                monotonic(prior, row)
            else:
                require(prior == row)
        return

    require(ds[0] in added and d["generation_before"] == old.attempt.expected_generation)
    require(d["generation_after"] == d["generation_before"] + 1)
    joins_before = {row.id: row for row in rows["joins"]}
    joins = {row.id: row for row in fresh["joins"]}
    added_joins, removed_joins, added_histories, changed_joins, changed_histories = set(), set(), set(), set(), set()
    lifecycle_effects, controls = {}, {}
    for item in d["results"]:
        key, workspace = item["membership_id"], item["workspace_id"]
        if item["membership_created"]:
            require(key not in old_histories and item["join_id"] not in joins_before)
            added_histories.add(key)
            added_joins.add(item["join_id"])
            lifecycle_effects[workspace] = "active"
        elif item["membership_regranted"]:
            prior = old_histories[key]
            require(prior.join_id not in joins_before and item["join_id"] not in joins_before)
            require(histories[key].ownership_epoch == prior.ownership_epoch + 1)
            added_joins.add(item["join_id"])
            changed_histories.add(key)
            lifecycle_effects[workspace] = "active"
            controls[key] = ("local_member_regrant", prior.join_id, item["join_id"], prior.ownership_epoch)
        elif item["ownership_decision"] == "managed_current":
            prior = old_histories[key]
            require(prior.join_id == item["join_id"] and prior.join_id in joins_before)
            changed = joins_before[prior.join_id].role != joins[prior.join_id].role
            require(item["role_changed"] is changed)
            if changed:
                require(item["outcome"] == "applied" and item["metadata_changed"] is True)
                changed_joins.add(prior.join_id)
                lifecycle_effects[workspace] = "role"
            changed_histories.add(key)
    for item in d.get("withdrawals", ()):
        key, prior = item["membership_id"], old_histories[item["membership_id"]]
        require(prior.join_id == item["removed_join_id"] and prior.ownership_epoch == item["epoch_before"])
        require(joins_before[prior.join_id].role.value == item["prior_role"])
        require(histories[key].ownership_epoch == item["epoch_after"])
        removed_joins.add(prior.join_id)
        changed_histories.add(key)
        lifecycle_effects[item["workspace_id"]] = "withdrawn"
        controls[key] = ("local_member_withdraw", prior.join_id, None, prior.ownership_epoch)
    require(set(joins_before) - set(joins) == removed_joins and set(joins) - set(joins_before) == added_joins)
    require(set(histories) - set(old_histories) == added_histories and set(old_histories) <= set(histories))
    for key, prior in joins_before.items():
        if key not in removed_joins:
            row = joins[key]
            same(prior, row, ("role", "updated_at") if key in changed_joins else ())
            if key in changed_joins:
                monotonic(prior, row)
    for key, prior in old_histories.items():
        row = histories[key]
        if key in changed_histories:
            mutable = {
                "desired_generation",
                "revision_id",
                "last_applied_roles_json",
                "last_applied_fingerprint",
                "desired_roles_json",
                "updated_at",
            }
            if key in controls:
                mutable.update(("join_id", "ownership_epoch", "finalization"))
            same(prior, row, mutable)
            require(row.desired_generation == d["generation_after"] and row.revision_id == refs["revision_id"])
            monotonic(prior, row)
        else:
            require(row == prior)
    from repositories.casdoor_terminal_retained_invitation_facts_extend import ACTION, needed, recovery_facts

    retained = tuple(row for row in added if row.action == ACTION)
    require(len(retained) == int(needed(d)))
    if retained:
        recovery_facts(retained[0], ds[0], d, rows, fresh)
    control_rows = tuple(row for row in added if row is not ds[0] and row not in retained)
    require(len(control_rows) == len(controls))
    operations = tuple(row for row in fresh["intents"] if row.id == operation)
    require(len(operations) == 1)
    matched = set()
    for row in control_rows:
        require(row.correlation_id == operation and row.actor_account_id is None and row.result_code == "ok")
        data = _strict_json(
            row.summary_json,
            {"schema_version", "references", "count", "epoch_before", "epoch_after", "generation", "fence_epoch"},
        )
        cr = data["references"]
        require(
            type(cr) is dict
            and set(cr)
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
        key = cr["membership_id"]
        require(key in controls and key not in matched)
        matched.add(key)
        require((row.action, cr["old_join_id"], cr["new_join_id"], data["epoch_before"]) == controls[key])
        require(cr["workspace_id"] == old_histories[key].workspace_id)
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            require(cr[field] == refs[field] == getattr(row, field))
        require(
            type(data["schema_version"]) is int
            and data["schema_version"] == 1
            and type(data["count"]) is int
            and data["count"] == 1
        )
        require(all(type(data[field]) is int for field in ("epoch_before", "epoch_after", "generation", "fence_epoch")))
        require(data["epoch_after"] == data["epoch_before"] + 1 == histories[key].ownership_epoch)
        require(data["generation"] == d["generation_after"] and data["fence_epoch"] == d["fence_epoch"])
        control_audit_interval(operations[0], row, ds[0])
    previous_lifecycles = {row.workspace_id: row for row in rows["lifecycles"]}
    lifecycles = {row.workspace_id: row for row in fresh["lifecycles"]}
    require(set(previous_lifecycles) <= set(lifecycles))
    require(set(lifecycles) - set(previous_lifecycles) <= set(lifecycle_effects))
    for workspace, row in lifecycles.items():
        prior = previous_lifecycles.get(workspace)
        effect = lifecycle_effects.get(workspace)
        if effect is None:
            require(row == prior)
        elif prior is None:
            require(
                row.account_id == refs["account_id"]
                and row.workspace_id == workspace
                and row.epoch == 1
                and row.state == "active"
            )
            require(
                type(row.created_at) is datetime and row.created_at.tzinfo is None and row.created_at <= row.updated_at
            )
            monotonic(row, row)
        else:
            same(prior, row, ("epoch", "state", "updated_at"))
            require(row.epoch == prior.epoch + 1 and row.state == (prior.state if effect == "role" else effect))
            monotonic(prior, row)
