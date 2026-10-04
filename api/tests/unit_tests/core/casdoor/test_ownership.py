"""Synthetic policy cases; supplied projections are not online backend proof."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from core.casdoor.ownership import (
    ExternalMemberRolesProjection,
    ManagedMembershipSnapshot,
    MemberRole,
    MembershipBackend,
    MembershipObservation,
    OwnershipDecision,
    RolesKnowledge,
    canonical_roles,
    decide_ownership,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorMembershipOwnership, CasdoorMembershipSource


def observation(*, backend=MembershipBackend.LOCAL, role=TenantAccountRole.NORMAL, roles=()):
    workspace, account = uuid4(), uuid4()
    return MembershipObservation(
        workspace,
        account,
        uuid4(),
        role,
        backend,
        ExternalMemberRolesProjection(workspace, account, RolesKnowledge.COMPLETE, roles),
    )


def managed(obs):
    baseline = role_baseline_json(obs)
    return ManagedMembershipSnapshot(
        uuid4(),
        uuid4(),
        uuid4(),
        obs.account_id,
        obs.workspace_id,
        obs.join_id,
        CasdoorMembershipOwnership.MANAGED,
        0,
        CasdoorMembershipSource.MAPPING,
        1,
        uuid4(),
        baseline,
        roles_fingerprint(obs),
        "{}",
        baseline,
        False,
    )


@pytest.mark.parametrize("role", list(TenantAccountRole))
def test_existing_local_join_never_adopted_even_with_equal_role(role):
    obs = observation(role=role)
    assert decide_ownership(obs, None) is (
        OwnershipDecision.OWNER_PROTECTED if role is TenantAccountRole.OWNER else OwnershipDecision.PRESERVE_UNMANAGED
    )


@pytest.mark.parametrize("ownership", [CasdoorMembershipOwnership.LOCAL_OVERRIDE, CasdoorMembershipOwnership.RELEASED])
@pytest.mark.parametrize("present", [True, False])
def test_local_handback_and_deleted_tombstone_cannot_rejoin(ownership, present):
    obs = observation()
    record = replace(managed(obs), ownership=ownership, tombstone=not present)
    if not present:
        obs = replace(obs, join_id=None, join_role=None)
    assert decide_ownership(obs, record) is (
        OwnershipDecision.PRESERVE_OVERRIDE
        if not present or ownership is CasdoorMembershipOwnership.LOCAL_OVERRIDE
        else OwnershipDecision.PRESERVE_UNMANAGED
    )


def test_join_recreation_same_role_is_override_not_reownership():
    obs = observation()
    record = managed(obs)
    assert decide_ownership(replace(obs, join_id=uuid4()), record) is OwnershipDecision.MARK_OVERRIDE_REQUIRED
    assert (
        decide_ownership(replace(obs, join_id=None, join_role=None), record) is OwnershipDecision.MARK_OVERRIDE_REQUIRED
    )
    assert (
        decide_ownership(replace(obs, join_role=TenantAccountRole.ADMIN), record)
        is OwnershipDecision.MARK_OVERRIDE_REQUIRED
    )
    assert decide_ownership(obs, record) is OwnershipDecision.MANAGED_CURRENT


@pytest.mark.parametrize(
    "is_builtin,category,tag,owner",
    [
        (True, "global_system_default", "owner", True),
        (False, "global_system_default", "owner", False),
        (True, "custom", "owner", False),
        (True, "global_system_default", "admin", False),
    ],
)
def test_remote_owner_exact_builtin_tag_protects_normal_join(is_builtin, category, tag, owner):
    obs = observation(backend=MembershipBackend.REMOTE, roles=(MemberRole("r1", is_builtin, category, tag),))
    assert decide_ownership(obs, managed(obs)) is (
        OwnershipDecision.REQUEST_OWNER_FENCE if owner else OwnershipDecision.MANAGED_CURRENT
    )
    assert decide_ownership(obs, None) is (
        OwnershipDecision.OWNER_PROTECTED if owner else OwnershipDecision.PRESERVE_UNMANAGED
    )


def test_local_owner_requests_fence_but_does_not_release_existing_record():
    obs = observation()
    record = managed(obs)
    assert (
        decide_ownership(replace(obs, join_role=TenantAccountRole.OWNER), record)
        is OwnershipDecision.REQUEST_OWNER_FENCE
    )
    assert record.ownership is CasdoorMembershipOwnership.MANAGED


@pytest.mark.parametrize(
    "change",
    [
        {"knowledge": RolesKnowledge.UNKNOWN},
        {"workspace_id": uuid4()},
        {"account_id": uuid4()},
        {"knowledge": "complete"},
        {"roles": []},
        {"roles": (MemberRole("r", "true", "", ""),)},
    ],
)
def test_unknown_partial_or_cross_owner_remote_never_controls(change):
    obs = observation(backend=MembershipBackend.REMOTE)
    record = managed(obs)
    broken = replace(obs, remote=replace(obs.remote, **change))
    assert decide_ownership(broken, record) is OwnershipDecision.AUTHORIZATION_PENDING
    assert (
        decide_ownership(replace(broken, join_id=None, join_role=None), None) is OwnershipDecision.AUTHORIZATION_PENDING
    )


def test_default_unknown_remote_blocks_new_join():
    obs = observation(backend=MembershipBackend.REMOTE)
    obs = replace(obs, join_id=None, join_role=None, remote=ExternalMemberRolesProjection())
    assert decide_ownership(obs, None) is OwnershipDecision.AUTHORIZATION_PENDING


def test_complete_remote_set_includes_custom_and_permission_metadata():
    normal = MemberRole("normal", True, "global_system_default", "normal", ("read", "write"))
    custom = MemberRole("custom", False, "workspace", "", ("custom.read",))
    obs = observation(backend=MembershipBackend.REMOTE, roles=(normal, custom))
    record = managed(obs)
    reordered = replace(
        obs, remote=replace(obs.remote, roles=(custom, replace(normal, permission_keys=("write", "read"))))
    )
    assert roles_fingerprint(reordered) == roles_fingerprint(obs)
    assert decide_ownership(reordered, record) is OwnershipDecision.MANAGED_CURRENT
    for roles in (
        (normal,),
        (normal, replace(custom, permission_keys=("custom.write",))),
        (replace(normal, role_tag="editor"), custom),
    ):
        assert (
            decide_ownership(replace(obs, remote=replace(obs.remote, roles=roles)), record)
            is OwnershipDecision.MARK_OVERRIDE_REQUIRED
        )
    assert len(json.loads(record.baseline_json)["roles"]) == 2


@pytest.mark.parametrize(
    "roles",
    [
        (MemberRole("same", False, "", ""), MemberRole("same", True, "", "")),
        (MemberRole("r", False, "", "", ("dup", "dup")),),
        (MemberRole("r", False, "", "", ["read"]),),
        (MemberRole("", False, "", ""),),
    ],
)
def test_malformed_role_sets_are_not_collapsed(roles):
    with pytest.raises(ValueError, match="authorization_pending"):
        canonical_roles(roles)


def test_preexisting_remote_grant_without_join_is_unmanaged():
    obs = observation(
        backend=MembershipBackend.REMOTE, roles=(MemberRole("admin", True, "global_system_default", "admin"),)
    )
    assert decide_ownership(replace(obs, join_id=None, join_role=None), None) is OwnershipDecision.PRESERVE_UNMANAGED


@pytest.mark.parametrize("backend", list(MembershipBackend))
def test_only_actual_absence_is_eligible_for_new_join(backend):
    obs = observation(backend=backend)
    assert decide_ownership(replace(obs, join_id=None, join_role=None), None) is OwnershipDecision.NEW_JOIN_REQUIRED


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", uuid4()),
        ("workspace_id", uuid4()),
        ("ownership", "managed"),
    ],
)
def test_cross_scope_or_untyped_record_is_never_current(field, value):
    obs = observation()
    assert decide_ownership(obs, replace(managed(obs), **{field: value})) is OwnershipDecision.AUTHORIZATION_PENDING


@pytest.mark.parametrize(
    "roles",
    [
        tuple(MemberRole(f"r{i}", False, "", "") for i in range(2049)),
        (MemberRole("中" * 700, False, "", ""),),
        (MemberRole("r", False, "", "", tuple(f"permission-{i}-" + "x" * 120 for i in range(500))),),
        (MemberRole("r", False, "", "", tuple(f"权限-{i}-" + "中" * 20 for i in range(500))),),
    ],
)
def test_over_budget_complete_roles_fail_closed_without_truncation(roles):
    obs = observation(backend=MembershipBackend.REMOTE, roles=roles)
    assert decide_ownership(replace(obs, join_id=None, join_role=None), None) is OwnershipDecision.AUTHORIZATION_PENDING
    with pytest.raises(ValueError, match="authorization_pending"):
        role_baseline_json(obs)


def test_total_permission_budget_is_bounded_across_roles():
    roles = tuple(MemberRole(f"r{i}", False, "", "", tuple(f"p{j}" for j in range(4097))) for i in range(2))
    with pytest.raises(ValueError, match="authorization_pending"):
        canonical_roles(roles)


def test_parse_baseline_is_unscoped_canonical_snapshot():
    from core.casdoor.ownership import parse_role_baseline_json

    for backend in MembershipBackend:
        source = role_baseline_json(observation(backend=backend))
        parsed = parse_role_baseline_json(source)
        assert parsed.backend is backend
        assert parsed.join_role is TenantAccountRole.NORMAL
        assert parsed.roles == ()
        assert not hasattr(parsed, "workspace_id") and not hasattr(parsed, "account_id")


@pytest.mark.parametrize(
    "mutation",
    [
        "bool",
        "float",
        "future",
        "duplicate",
        "whitespace",
        "backend",
        "missing_join",
        "partial_role",
        "duplicate_role",
        "permissions",
        "metadata",
        "nan",
        "unicode",
        "bytes",
        "deep",
        "local_roles",
    ],
)
def test_strict_baseline_parser_rejects_noncanonical_or_incomplete(mutation):
    from core.casdoor.ownership import MAX_SNAPSHOT_BYTES, parse_role_baseline_json

    data = json.loads(role_baseline_json(observation(backend=MembershipBackend.REMOTE)))
    role = dict(role_id="custom", is_builtin=False, category="custom", role_tag="", permission_keys=[])
    if mutation in ("bool", "float", "future"):
        data["schema_version"] = {"bool": True, "float": 1.0, "future": 2}[mutation]
    elif mutation == "backend":
        data["backend"] = "unsupported"
    elif mutation == "missing_join":
        del data["join_role"]
    elif mutation == "partial_role":
        data["roles"] = [{"role_id": "partial"}]
    elif mutation == "duplicate_role":
        data["roles"] = [role, role]
    elif mutation == "permissions":
        role["permission_keys"] = ["read"] * 8193
        data["roles"] = [role]
    elif mutation == "metadata":
        role["role_tag"] = "owner"
        data["roles"] = [role]
    elif mutation == "local_roles":
        data.update(backend="local", roles=[role])
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if mutation == "duplicate":
        text = text[:-1] + ',"schema_version":1}'
    elif mutation == "whitespace":
        text += " "
    elif mutation == "nan":
        text = text.replace('"schema_version":1', '"schema_version":NaN')
    elif mutation == "unicode":
        text = "\ud800"
    elif mutation == "bytes":
        text = "汉" * (MAX_SNAPSHOT_BYTES // 3 + 1)
    elif mutation == "deep":
        text = "[" * 2000 + "]" * 2000
    with pytest.raises(ValueError, match="authorization_pending"):
        parse_role_baseline_json(text)


def test_baseline_byte_guard_runs_before_json(monkeypatch):
    from core.casdoor import ownership

    source = role_baseline_json(observation())
    monkeypatch.setattr(ownership, "MAX_SNAPSHOT_BYTES", len(source.encode()))
    assert ownership.parse_role_baseline_json(source).backend is MembershipBackend.LOCAL
    monkeypatch.setattr(ownership.json, "loads", lambda *_a, **_k: pytest.fail("decoded over budget"))
    with pytest.raises(ValueError, match="authorization_pending"):
        ownership.parse_role_baseline_json(source + " ")


def test_baseline_parser_reuses_aggregate_permission_and_role_byte_budgets(monkeypatch):
    from core.casdoor import ownership

    roles = (
        MemberRole("a", False, "custom", "", ("read", "write")),
        MemberRole("b", False, "custom", "", ("list",)),
    )
    source = role_baseline_json(observation(backend=MembershipBackend.REMOTE, roles=roles))
    with monkeypatch.context() as patcher:
        patcher.setattr(ownership, "MAX_PERMISSION_KEYS", 3)
        assert len(ownership.parse_role_baseline_json(source).roles) == 2
        patcher.setattr(ownership, "MAX_PERMISSION_KEYS", 2)
        with pytest.raises(ValueError, match="authorization_pending"):
            ownership.parse_role_baseline_json(source)
    monkeypatch.setattr(ownership, "MAX_ROLE_SET_BYTES", 2)
    with pytest.raises(ValueError, match="authorization_pending"):
        ownership.parse_role_baseline_json(source)
