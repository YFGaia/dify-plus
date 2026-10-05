"""Private immutable LOCAL facts from the real live D producer, never authority.

Old D/F bytes stay unchanged. Missing facts cannot be backfilled from today's
rows. The closed capture has at most 100 retained histories and 32 KiB total bytes;
larger valid plans fail closed rather than truncate. This bounded SQL observation
assumes trusted persistence and proves no
current membership permission, writer capability or session admission.
"""

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.invited_controlled_write_receipt import controlled_finalization_ids
from core.casdoor.invited_finalization_receipt import FULL_HISTORY_FIELDS, mapping_plan_projection, mapping_plan_sha256
from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MEMBERSHIP_FIELDS,
    InvitedLocalWriteReceipt,
    postwrite_projection,
    postwrite_sha256,
)
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    parse_local_withdrawal_json,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorAuditExtend as Audit
from repositories.casdoor_invitation_operation_repository_extend import _canonical, _digest, _now, _strict_json, _uuid
from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository, _time
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict

ACTION = "invited_local_retained_history"
CAP = 32768
JOIN_FULL = (
    "id",
    "tenant_id",
    "account_id",
    "current",
    "role",
    "invited_by",
    "created_at",
    "updated_at",
    "last_opened_at",
)
FIELDS = {
    "schema_version",
    "receipt_kind",
    "references",
    "write_audit_id",
    "write_receipt_sha256",
    "generation",
    "fence_epoch",
    "plan",
    "postwrite",
    "histories",
}
PAIR_FIELDS = {"before", "after", "join_before", "join_after"}


def require(value):
    if not value:
        raise CasdoorLoginScopeConflict()


def primitive(row, fields):
    result = {}
    require(set(row._mapping) >= set(fields))
    for key in fields:
        value = getattr(row, key)
        if isinstance(value, StrEnum):
            value = value.value
        elif isinstance(value, datetime):
            value = _time(value).isoformat(timespec="microseconds") + "Z"
        result[key] = value
    return result


def needed(d):
    return bool(d.get("withdrawals")) or any(
        not item["membership_created"] and item["membership_id"] is not None for item in d["results"]
    )


def predicate(operation_id, write_audit_id):
    # Exact related-body navigation also catches a foreign/mislabeled header.
    # Navigation is not proof: every full row and its canonical body is verified.
    return sa.and_(
        sa.or_(
            Audit.action == ACTION,
            Audit.summary_json.contains('"receipt_kind":"' + ACTION + '"'),
            Audit.correlation_id == write_audit_id,
            Audit.summary_json.contains('"write_audit_id":"' + write_audit_id + '"'),
        ),
        sa.or_(
            Audit.correlation_id == operation_id,
            Audit.correlation_id == write_audit_id,
            Audit.summary_json.contains('"operation_id":"' + operation_id + '"'),
            Audit.summary_json.contains('"write_audit_id":"' + write_audit_id + '"'),
        ),
    )


def read_rows(reader, operation_id, write_audit_id):
    _uuid(operation_id)
    _uuid(write_audit_id)
    return reader._bounded_rows(Audit, predicate(operation_id, write_audit_id), cap=1, text_limit=CAP)


def build(value):
    """Only captured before rows, sealed plan and already-verified actual B3 rows."""
    receipt = value.receipt
    require(type(receipt) is InvitedLocalWriteReceipt)
    d = receipt.values()
    refs = d["references"]
    before, after = value.rows, value.verified_rows
    old = {row.id: row for row in before["histories"]}
    current = {row.id: row for row in after["histories"]}
    ids = {
        item["membership_id"]
        for item in d["results"]
        if not item["membership_created"] and item["membership_id"] is not None
    }
    ids.update(item["membership_id"] for item in d.get("withdrawals", ()))
    before_joins, after_joins = ({row.id: row for row in rows["joins"]} for rows in (before, after))
    require(ids <= set(old) & set(current) and len(ids) <= 100)
    pairs = []
    for key in sorted(ids):
        prior, actual = old[key], current[key]
        pairs.append(
            {
                "before": primitive(prior, FULL_HISTORY_FIELDS),
                "after": primitive(actual, FULL_HISTORY_FIELDS),
                "join_before": primitive(before_joins[prior.join_id], JOIN_FULL)
                if prior.join_id in before_joins
                else None,
                "join_after": primitive(after_joins[actual.join_id], JOIN_FULL)
                if actual.join_id in after_joins
                else None,
            }
        )
    identity = next(row for row in after["identities"] if row.id == refs["identity_id"])
    return {
        "schema_version": 1,
        "receipt_kind": ACTION,
        "references": refs,
        "write_audit_id": value.audit_row.id,
        "write_receipt_sha256": sha256(receipt.canonical_json.encode()).hexdigest(),
        "generation": d["generation_after"],
        "fence_epoch": d["fence_epoch"],
        "plan": mapping_plan_projection(
            tuple(
                {
                    "workspace_id": str(t.workspace_id),
                    "target_role": t.target_role,
                    "builtin_id": t.builtin_id,
                    "reason": t.reason.value,
                }
                for t in value.plan.targets
            )
        ),
        "postwrite": postwrite_projection(
            identity=primitive(identity, IDENTITY_FIELDS),
            joins=tuple(primitive(r, JOIN_FIELDS) for r in after["joins"]),
            memberships=tuple(primitive(r, MEMBERSHIP_FIELDS) for r in after["histories"]),
        ),
        "histories": pairs,
    }


def append_actual(owner, guard):
    """No DTO argument: exact registry owner/root/phase and real D append only."""
    from repositories.casdoor_invited_login_guard_repository_extend import CasdoorInvitedLoginGuardRepository, _binding

    value = _binding(guard, owner.session)
    require(type(owner) is CasdoorInvitedLoginGuardRepository and value.owner is owner)
    require(value.phase == "receipting" and value.receipt_append_started and value.audit_row is not None)
    reader = CasdoorInvitedWriteReceiptRepository(owner.session, owner.configuration_factory)
    d = value.receipt.values()
    rows = read_rows(reader, d["references"]["operation_id"], value.audit_row.id)
    require(not rows)
    if not needed(d):
        return None
    owner._recheck_verified(value)
    from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository

    CasdoorAuditRepository(owner.session)._read_invited_write_receipt(value.receipt, dict(value.audit_row._mapping))
    data = build(value)
    raw = _canonical(data)
    require(len(raw.encode()) <= CAP)
    refs = d["references"]
    expected = dict(
        id=str(uuid4()),
        created_at=_now(),
        namespace_id=refs["namespace_id"],
        revision_id=refs["revision_id"],
        identity_id=refs["identity_id"],
        account_id=refs["account_id"],
        actor_account_id=None,
        action=ACTION,
        result_code="verified",
        correlation_id=value.audit_row.id,
        summary_json=raw,
    )
    owner.session.add(Audit(**expected))
    owner.session.flush()
    reread_actual(owner, guard, expected)
    return expected


def reread_actual(owner, guard, expected):
    from repositories.casdoor_invited_login_guard_repository_extend import _binding

    value = _binding(guard, owner.session)
    require(value.owner is owner and value.phase == "receipting" and value.receipt_append_started)
    reader = CasdoorInvitedWriteReceiptRepository(owner.session, owner.configuration_factory)
    rows = read_rows(reader, value.receipt.values()["references"]["operation_id"], value.audit_row.id)
    if expected is None:
        require(not rows and not needed(value.receipt.values()))
    else:
        require(len(rows) == 1 and dict(rows[0]._mapping) == expected)
        require(rows[0].summary_json == _canonical(build(value)))


def stamp(raw):
    require(type(raw) is str and len(raw) == 27 and raw.endswith("Z"))
    value = datetime.fromisoformat(raw[:-1])
    require(value.isoformat(timespec="microseconds") + "Z" == raw)
    return value


def row_shape(row, fields, refs, *, join=False):
    require(type(row) is dict and set(row) == set(fields))
    for key in (
        ("id", "tenant_id", "account_id")
        if join
        else ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "join_id", "revision_id")
    ):
        _uuid(row[key])
    require(row["account_id"] == refs["account_id"])
    require(stamp(row["created_at"]) <= stamp(row["updated_at"]))
    if join:
        require(
            type(row["current"]) is bool and row["role"] in ("owner", "admin", "editor", "normal", "dataset_operator")
        )
        if row["invited_by"] is not None:
            _uuid(row["invited_by"])
        if row["last_opened_at"] is not None:
            stamp(row["last_opened_at"])
        return
    require(row["namespace_id"] == refs["namespace_id"] and row["identity_id"] == refs["identity_id"])
    require(row["source"] in ("fallback", "mapping") and row["ownership"] in ("managed", "local_override"))
    require(row["finalization"] in ("pending", "finalized") and row["tombstone"] is False)
    for field in ("ownership_epoch", "desired_generation"):
        require(type(row[field]) is int and 0 <= row[field] <= 2**63 - 1)
    _digest(row["last_applied_fingerprint"])
    for field in ("baseline_json", "last_applied_roles_json"):
        require(parse_role_baseline_json(row[field]).backend is MembershipBackend.LOCAL)


def same(before, after, mutable=()):
    require(
        set(before) == set(after) and all(after[key] == value for key, value in before.items() if key not in mutable)
    )
    require(stamp(before["updated_at"]) <= stamp(after["updated_at"]))


def applied(row, join, refs, d, target):
    require(row["revision_id"] == refs["revision_id"] and row["desired_generation"] == d["generation_after"])
    require(row["ownership"] == "managed" and row["join_id"] == join["id"])
    require(join["role"] == target["target_role"] and join["invited_by"] is None)
    observation = MembershipObservation(
        UUID(row["workspace_id"]),
        UUID(refs["account_id"]),
        UUID(join["id"]),
        TenantAccountRole(join["role"]),
        MembershipBackend.LOCAL,
    )
    require(
        row["last_applied_roles_json"] == role_baseline_json(observation)
        and row["last_applied_fingerprint"] == roles_fingerprint(observation)
    )
    require(
        row["desired_roles_json"]
        == _canonical(
            dict(
                schema_version=1,
                backend="local",
                target_role=target["target_role"],
                builtin_id=target["builtin_id"],
                role_ids=[target["target_role"]],
                reason=target["reason"],
                fence_epoch=d["fence_epoch"],
            )
        )
    )


def verify(row, d_audit, d, *, configuration=None, operation=None, f=None, f_audit=None):
    """Validate immutable historical domain only, never compare to today's roles."""
    _uuid(row.id)
    data = _strict_json(row.summary_json, FIELDS)
    refs = d["references"]
    require(type(data["schema_version"]) is int and data["schema_version"] == 1 and data["receipt_kind"] == ACTION)
    require(data["references"] == refs and row.correlation_id == d_audit.id and row.action == ACTION)
    require(row.actor_account_id is None and row.result_code == "verified")
    for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
        require(getattr(row, field) == refs[field])
    require(
        data["write_audit_id"] == d_audit.id
        and data["write_receipt_sha256"] == sha256(d_audit.summary_json.encode()).hexdigest()
    )
    require(type(data["generation"]) is int and type(data["fence_epoch"]) is int)
    require((data["generation"], data["fence_epoch"]) == (d["generation_after"], d["fence_epoch"]))
    require(_time(d_audit.created_at) <= _time(row.created_at) <= _now())
    plan = mapping_plan_projection(tuple(data["plan"]))
    require([t["workspace_id"] for t in plan] == [r["workspace_id"] for r in d["results"]])
    targets = {item["workspace_id"]: item for item in plan}
    if configuration is not None:
        require(str(configuration.default_workspace_id) in targets)
        mappings = {str(m.workspace_id): m for m in configuration.workspace_mappings}
        for target in plan:
            role, reason, workspace = target["target_role"], target["reason"], target["workspace_id"]
            if reason == "default_normal_fallback":
                require(workspace == str(configuration.default_workspace_id) and role == "normal")
            else:
                require(workspace in mappings and getattr(mappings[workspace], role) is not None)
    p = data["postwrite"]
    require(type(p) is dict and set(p) == {"identity", "joins", "memberships"})
    require(
        postwrite_projection(identity=p["identity"], joins=tuple(p["joins"]), memberships=tuple(p["memberships"])) == p
    )
    require(
        p["identity"]
        == dict(
            id=refs["identity_id"],
            namespace_id=refs["namespace_id"],
            account_id=refs["account_id"],
            sync_generation=d["generation_after"],
        )
    )
    require(
        postwrite_sha256(
            refs["operation_id"], identity=p["identity"], joins=tuple(p["joins"]), memberships=tuple(p["memberships"])
        )
        == d["postwrite_sha256"]
    )
    pairs = data["histories"]
    require(type(pairs) is list and 1 <= len(pairs) <= 100)
    expected_ids = {
        item["membership_id"]
        for item in d["results"]
        if not item["membership_created"] and item["membership_id"] is not None
    } | {item["membership_id"] for item in d.get("withdrawals", ())}
    require([item["before"]["id"] for item in pairs] == sorted(expected_ids))
    details = {}
    joins = {r["id"]: r for r in p["joins"]}
    histories = {r["id"]: r for r in p["memberships"]}
    for pair in pairs:
        require(type(pair) is dict and set(pair) == PAIR_FIELDS)
        prior, actual = pair["before"], pair["after"]
        row_shape(prior, FULL_HISTORY_FIELDS, refs)
        row_shape(actual, FULL_HISTORY_FIELDS, refs)
        require(prior["id"] == actual["id"] and prior["finalization"] == "finalized")
        require(
            prior["desired_generation"] <= d["generation_before"]
            and stamp(actual["updated_at"]) <= _time(d_audit.created_at)
        )
        require(actual["id"] in histories and {k: actual[k] for k in MEMBERSHIP_FIELDS} == histories[actual["id"]])
        for join in (pair["join_before"], pair["join_after"]):
            if join is not None:
                row_shape(join, JOIN_FULL, refs, join=True)
                require(join["tenant_id"] == actual["workspace_id"])
        if pair["join_before"] is not None:
            require(pair["join_before"]["id"] == prior["join_id"])
        if pair["join_after"] is not None:
            require(
                pair["join_after"]["id"] == actual["join_id"]
                and {k: pair["join_after"][k] for k in JOIN_FIELDS} == joins.get(actual["join_id"])
            )
        details[prior["id"]] = pair
    mutable = {
        "revision_id",
        "desired_generation",
        "last_applied_roles_json",
        "last_applied_fingerprint",
        "desired_roles_json",
        "updated_at",
    }
    for item in d["results"]:
        require(item["join_id"] in joins)
        require(
            joins[item["join_id"]]["tenant_id"] == item["workspace_id"]
            and joins[item["join_id"]]["role"] == item["current_role"]
        )
        if item["membership_id"] is not None:
            require(item["membership_id"] in histories)
            h = histories[item["membership_id"]]
            require(
                (h["namespace_id"], h["identity_id"], h["account_id"], h["workspace_id"], h["join_id"])
                == (
                    refs["namespace_id"],
                    refs["identity_id"],
                    refs["account_id"],
                    item["workspace_id"],
                    item["join_id"],
                )
            )
        if item["membership_created"]:
            require(
                item["ownership_decision"] == "new_join_required"
                and item["outcome"] == "applied"
                and not item["role_changed"]
                and item["metadata_changed"]
            )
            continue
        if item["membership_id"] is None:
            require(item["outcome"] == "preserved" and not item["role_changed"] and not item["metadata_changed"])
            require(
                item["ownership_decision"]
                == ("owner_protected" if item["current_role"] == "owner" else "preserve_unmanaged")
            )
            continue
        pair = details[item["membership_id"]]
        prior, actual, bj, aj = (pair[k] for k in ("before", "after", "join_before", "join_after"))
        require(aj is not None and aj["id"] == item["join_id"] and aj["role"] == item["current_role"])
        if item["membership_regranted"]:
            require(
                bj is None
                and prior["ownership"] == "managed"
                and actual["ownership_epoch"] == prior["ownership_epoch"] + 1
            )
            marker = parse_local_withdrawal_json(prior["desired_roles_json"])
            require(
                marker["removed_join_id"] == prior["join_id"]
                and marker["withdrawal_epoch"] == prior["ownership_epoch"]
                and marker["withdrawal_generation"] == prior["desired_generation"]
            )
            absence = MembershipObservation(
                UUID(prior["workspace_id"]), UUID(refs["account_id"]), None, None, MembershipBackend.LOCAL
            )
            require(
                prior["last_applied_roles_json"] == role_baseline_json(absence)
                and prior["last_applied_fingerprint"] == roles_fingerprint(absence)
            )
            same(prior, actual, mutable | {"join_id", "ownership_epoch", "finalization"})
            require(prior["join_id"] != actual["join_id"] and actual["finalization"] == "pending")
            applied(actual, aj, refs, d, targets[item["workspace_id"]])
        elif item["ownership_decision"] == "managed_current":
            require(
                bj is not None
                and prior["ownership"] == actual["ownership"] == "managed"
                and actual["finalization"] == "finalized"
            )
            require(item["outcome"] in ("applied", "noop") and item["role_changed"] == (bj["role"] != aj["role"]))
            if item["role_changed"]:
                require(item["outcome"] == "applied" and item["metadata_changed"])
            same(prior, actual, mutable)
            same(bj, aj, ("role", "updated_at") if item["role_changed"] else ())
            observation = MembershipObservation(
                UUID(prior["workspace_id"]),
                UUID(refs["account_id"]),
                UUID(bj["id"]),
                TenantAccountRole(bj["role"]),
                MembershipBackend.LOCAL,
            )
            require(
                prior["last_applied_roles_json"] == role_baseline_json(observation)
                and prior["last_applied_fingerprint"] == roles_fingerprint(observation)
            )
            require(item["metadata_changed"] == any(actual[k] != prior[k] for k in mutable - {"updated_at"}))
            applied(actual, aj, refs, d, targets[item["workspace_id"]])
        else:
            require(item["outcome"] == "preserved" and not item["role_changed"] and not item["metadata_changed"])
            require(
                item["ownership_decision"] in ("owner_protected", "preserve_override") and prior == actual and bj == aj
            )
            require(item["ownership_decision"] == ("owner_protected" if aj["role"] == "owner" else "preserve_override"))
            require(actual["ownership"] == "local_override")
    for item in d.get("withdrawals", ()):
        pair = details[item["membership_id"]]
        prior, actual, bj, aj = (pair[k] for k in ("before", "after", "join_before", "join_after"))
        require(
            bj is not None and aj is None and bj["id"] == item["removed_join_id"] and bj["role"] == item["prior_role"]
        )
        require(prior["ownership"] == "managed" and bj["role"] in ("admin", "editor", "normal"))
        prior_observation = MembershipObservation(
            UUID(prior["workspace_id"]),
            UUID(refs["account_id"]),
            UUID(bj["id"]),
            TenantAccountRole(bj["role"]),
            MembershipBackend.LOCAL,
        )
        require(
            prior["last_applied_roles_json"] == role_baseline_json(prior_observation)
            and prior["last_applied_fingerprint"] == roles_fingerprint(prior_observation)
        )
        same(prior, actual, mutable | {"ownership_epoch", "finalization"})
        require((prior["ownership_epoch"], actual["ownership_epoch"]) == (item["epoch_before"], item["epoch_after"]))
        require(
            actual["ownership"] == "managed"
            and actual["finalization"] == "pending"
            and actual["desired_generation"] == d["generation_after"]
            and actual["revision_id"] == refs["revision_id"]
        )
        marker = parse_local_withdrawal_json(actual["desired_roles_json"])
        require(
            marker
            == dict(
                schema_version=2,
                backend="local",
                operation="controlled_withdrawal",
                removed_join_id=item["removed_join_id"],
                withdrawal_generation=item["generation"],
                withdrawal_epoch=item["epoch_after"],
                fence_epoch=item["fence_epoch"],
            )
        )
        require(
            item["removed_join_id"] not in joins
            and item["workspace_id"] not in {r["tenant_id"] for r in joins.values()}
        )
        absence = MembershipObservation(
            UUID(actual["workspace_id"]), UUID(refs["account_id"]), None, None, MembershipBackend.LOCAL
        )
        require(
            actual["last_applied_roles_json"] == role_baseline_json(absence)
            and actual["last_applied_fingerprint"] == roles_fingerprint(absence)
        )
    if f is not None:
        require(_time(row.created_at) <= _time(f_audit.created_at) <= _now())
        require(f["plan_sha256"] == mapping_plan_sha256(refs["operation_id"], tuple(plan)))
        ids = controlled_finalization_ids(d)
        require(f["finalized_ids"] == ids)
        require(all(key in histories and histories[key]["finalization"] == "pending" for key in ids))
        memberships = tuple(dict(r, finalization="finalized") if r["id"] in ids else dict(r) for r in p["memberships"])
        require(
            f["postwrite_sha256"]
            == postwrite_sha256(
                refs["operation_id"], identity=p["identity"], joins=tuple(p["joins"]), memberships=memberships
            )
        )
    return data


def terminal_facts(reader, d_audit, d, f_audit, f, operation, configuration):
    rows = read_rows(reader, d["references"]["operation_id"], d_audit.id)
    if not rows:
        return None
    require(needed(d))
    data = verify(rows[0], d_audit, d, configuration=configuration, operation=operation, f=f, f_audit=f_audit)
    refs = d["references"]
    expected = {}
    pairs = {p["before"]["id"]: p for p in data["histories"]}
    for item in d["results"]:
        if item["membership_regranted"]:
            p = pairs[item["membership_id"]]
            expected[item["membership_id"]] = (
                "local_member_regrant",
                p["before"]["join_id"],
                item["join_id"],
                p["before"]["ownership_epoch"],
                item["workspace_id"],
            )
    for item in d.get("withdrawals", ()):
        expected[item["membership_id"]] = (
            "local_member_withdraw",
            item["removed_join_id"],
            None,
            item["epoch_before"],
            item["workspace_id"],
        )
    related = (
        sa.and_(
            Audit.summary_json.contains('"generation":' + str(d["generation_after"]) + ","),
            sa.or_(*(Audit.summary_json.contains('"membership_id":"' + key + '"') for key in expected)),
        )
        if expected
        else sa.false()
    )
    controls = reader._bounded_rows(
        Audit,
        sa.or_(
            Audit.action.in_(("local_member_withdraw", "local_member_regrant")),
            Audit.summary_json.contains('"old_join_id":'),
        ),
        sa.or_(Audit.correlation_id == refs["operation_id"], related),
        cap=100,
        text_limit=CAP,
    )
    require(len(controls) == len(expected))
    seen = set()
    for row in controls:
        _uuid(row.id)
        c = _strict_json(
            row.summary_json,
            {"schema_version", "references", "count", "epoch_before", "epoch_after", "generation", "fence_epoch"},
        )
        cr = c["references"]
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
        require(key in expected and key not in seen)
        seen.add(key)
        require(
            (row.action, cr["old_join_id"], cr["new_join_id"], c["epoch_before"], cr["workspace_id"]) == expected[key]
        )
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            require(cr[field] == refs[field] == getattr(row, field))
        require(row.correlation_id == refs["operation_id"] and row.actor_account_id is None and row.result_code == "ok")
        require(
            type(c["schema_version"]) is int
            and c["schema_version"] == 1
            and type(c["count"]) is int
            and c["count"] == 1
        )
        require(
            all(
                type(c[k]) is int and 0 <= c[k] <= 2**63 - 1
                for k in ("epoch_before", "epoch_after", "generation", "fence_epoch")
            )
        )
        require(
            c["epoch_after"] == c["epoch_before"] + 1
            and c["generation"] == d["generation_after"]
            and c["fence_epoch"] == d["fence_epoch"]
        )
        require(_time(operation.terminated_at) <= _time(row.created_at) <= _time(d_audit.created_at))
    return (*rows, *controls)


def recovery_facts(row, d_audit, d, before, after):
    data = verify(row, d_audit, d)
    old, current = ({r.id: r for r in rows["histories"]} for rows in (before, after))
    old_joins, new_joins = ({r.id: r for r in rows["joins"]} for rows in (before, after))
    for pair in data["histories"]:
        key = pair["before"]["id"]
        require(key in old and key in current)
        require(
            pair["before"] == primitive(old[key], FULL_HISTORY_FIELDS)
            and pair["after"] == primitive(current[key], FULL_HISTORY_FIELDS)
        )
        for side, joins in (("before", old_joins), ("after", new_joins)):
            join = joins.get(pair[side]["join_id"])
            require(pair["join_" + side] == (primitive(join, JOIN_FULL) if join is not None else None))
