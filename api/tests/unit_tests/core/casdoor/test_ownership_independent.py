"""Independent adversarial checks for the I11-B read-only ownership policy."""

from dataclasses import replace
from uuid import uuid4

import pytest
from core.casdoor.ownership import (
    MAX_SNAPSHOT_BYTES,
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


def sample(backend=MembershipBackend.LOCAL, role=TenantAccountRole.NORMAL, remote_roles=()):
    workspace_id, account_id = uuid4(), uuid4()
    return MembershipObservation(
        workspace_id,
        account_id,
        uuid4() if role is not None else None,
        role,
        backend,
        ExternalMemberRolesProjection(workspace_id, account_id, RolesKnowledge.COMPLETE, remote_roles),
    )


def managed_for(observation):
    baseline = role_baseline_json(observation)
    return ManagedMembershipSnapshot(
        uuid4(),
        uuid4(),
        uuid4(),
        observation.account_id,
        observation.workspace_id,
        observation.join_id,
        CasdoorMembershipOwnership.MANAGED,
        3,
        CasdoorMembershipSource.MAPPING,
        5,
        uuid4(),
        baseline,
        roles_fingerprint(observation),
        '{"role_ids":["builtin-admin"]}',
        baseline,
        False,
    )


@pytest.mark.parametrize("role", list(TenantAccountRole))
def test_existing_join_is_never_claimed_by_matching_role(role):
    observation = sample(role=role)
    expected = (
        OwnershipDecision.OWNER_PROTECTED if role is TenantAccountRole.OWNER else OwnershipDecision.PRESERVE_UNMANAGED
    )
    assert decide_ownership(observation, None) is expected


@pytest.mark.parametrize(
    "metadata,protected",
    [
        ({"is_builtin": True, "category": "global_system_default", "role_tag": "owner"}, True),
        ({"is_builtin": False, "category": "global_system_default", "role_tag": "owner"}, False),
        ({"is_builtin": True, "category": "workspace", "role_tag": "owner"}, False),
        ({"is_builtin": True, "category": "global_system_default", "role_tag": "admin"}, False),
    ],
)
def test_remote_owner_requires_all_three_exact_metadata_fields(metadata, protected):
    projection = (MemberRole("r-owner", permission_keys=("read",), **metadata),)
    observation = sample(MembershipBackend.REMOTE, remote_roles=projection)
    active = managed_for(observation)
    assert decide_ownership(observation, active) is (
        OwnershipDecision.REQUEST_OWNER_FENCE if protected else OwnershipDecision.MANAGED_CURRENT
    )
    assert active.ownership is CasdoorMembershipOwnership.MANAGED
    assert decide_ownership(observation, None) is (
        OwnershipDecision.OWNER_PROTECTED if protected else OwnershipDecision.PRESERVE_UNMANAGED
    )


@pytest.mark.parametrize(
    "projection_change",
    [
        {"knowledge": RolesKnowledge.UNKNOWN},
        {"workspace_id": uuid4()},
        {"account_id": uuid4()},
        {"roles": (MemberRole("r", True, "global_system_default", ""),)},
    ],
)
def test_unknown_or_wrong_scope_remote_set_never_allows_replacement(projection_change):
    observation = sample(
        MembershipBackend.REMOTE,
        remote_roles=(MemberRole("known-role", False, "workspace", "reader"),),
    )
    record = managed_for(observation)
    observation = replace(observation, remote=replace(observation.remote, **projection_change))
    # Empty category/tag metadata is structurally valid in this pure handoff;
    # I13 must prove those keys were present in the raw response.
    expected = (
        OwnershipDecision.MARK_OVERRIDE_REQUIRED
        if "roles" in projection_change
        else OwnershipDecision.AUTHORIZATION_PENDING
    )
    assert decide_ownership(observation, record) is expected
    no_join = replace(observation, join_id=None, join_role=None)
    assert decide_ownership(no_join, None) is (
        OwnershipDecision.PRESERVE_UNMANAGED
        if "roles" in projection_change
        else OwnershipDecision.AUTHORIZATION_PENDING
    )


def test_remote_grants_without_local_join_are_preserved_as_unmanaged():
    observation = sample(
        MembershipBackend.REMOTE,
        role=None,
        remote_roles=(MemberRole("rbac-admin", True, "global_system_default", "admin"),),
    )
    assert decide_ownership(observation, None) is OwnershipDecision.PRESERVE_UNMANAGED


def test_full_remote_baseline_fingerprints_custom_roles_and_permissions_order_independently():
    builtin = MemberRole("builtin-admin", True, "global_system_default", "admin", ("write", "read"))
    custom = MemberRole("custom-role", False, "workspace", "", ("custom.view",))
    observation = sample(MembershipBackend.REMOTE, remote_roles=(builtin, custom))
    active = managed_for(observation)
    reordered = replace(
        observation,
        remote=replace(
            observation.remote,
            roles=(replace(custom), replace(builtin, permission_keys=("read", "write"))),
        ),
    )
    assert roles_fingerprint(reordered) == roles_fingerprint(observation)
    assert decide_ownership(reordered, active) is OwnershipDecision.MANAGED_CURRENT
    for altered in (
        (builtin,),
        (builtin, replace(custom, permission_keys=("custom.edit",))),
        (replace(builtin, role_tag="editor"), custom),
    ):
        assert (
            decide_ownership(replace(observation, remote=replace(observation.remote, roles=altered)), active)
            is OwnershipDecision.MARK_OVERRIDE_REQUIRED
        )


@pytest.mark.parametrize(
    "change",
    [
        {"join_id": uuid4()},
        {"join_id": None, "join_role": None},
        {"join_role": TenantAccountRole.ADMIN},
    ],
)
def test_local_baseline_mismatch_requests_override_without_mutating_record(change):
    observation = sample()
    active = managed_for(observation)
    result = decide_ownership(replace(observation, **change), active)
    assert result is OwnershipDecision.MARK_OVERRIDE_REQUIRED
    assert active.ownership is CasdoorMembershipOwnership.MANAGED
    assert active.ownership_epoch == 3


@pytest.mark.parametrize(
    "ownership,tombstone,expected",
    [
        (CasdoorMembershipOwnership.RELEASED, False, OwnershipDecision.PRESERVE_UNMANAGED),
        (CasdoorMembershipOwnership.LOCAL_OVERRIDE, False, OwnershipDecision.PRESERVE_OVERRIDE),
        (CasdoorMembershipOwnership.MANAGED, True, OwnershipDecision.PRESERVE_OVERRIDE),
    ],
)
def test_historical_release_or_tombstone_blocks_implicit_reownership(ownership, tombstone, expected):
    observation = sample()
    record = replace(managed_for(observation), ownership=ownership, tombstone=tombstone)
    absent = replace(observation, join_id=None, join_role=None)
    assert decide_ownership(absent, record) is expected


def test_role_count_permission_count_and_escaped_utf8_budgets_reject_without_truncation():
    oversized_role_count = tuple(MemberRole(f"r{i}", False, "", "") for i in range(2049))
    oversized_permissions = tuple(
        MemberRole(f"r{i}", False, "", "", tuple(f"p{j}" for j in range(4097))) for i in range(2)
    )
    oversized_utf8 = (MemberRole("权限" * 350, False, "workspace", "custom"),)
    for roles in (oversized_role_count, oversized_permissions, oversized_utf8):
        with pytest.raises(ValueError, match="authorization_pending"):
            canonical_roles(roles)


def test_canonical_json_stays_within_mysql_text_limit_and_oversize_is_rejected():
    accepted = tuple(MemberRole(f"role-{i}-" + "x" * 1750, False, "workspace", "") for i in range(30))
    observation = sample(MembershipBackend.REMOTE, remote_roles=accepted)
    serialized = role_baseline_json(observation)
    assert len(serialized.encode("utf-8")) <= MAX_SNAPSHOT_BYTES
    oversized = tuple(MemberRole(f"role-{i}-" + "x" * 1950, False, "workspace", "") for i in range(32))
    with pytest.raises(ValueError, match="authorization_pending"):
        role_baseline_json(sample(MembershipBackend.REMOTE, remote_roles=oversized))
