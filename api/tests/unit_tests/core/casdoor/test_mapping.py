"""Desired policy only; synthetic server projections are not G0-B proofs."""

from dataclasses import FrozenInstanceError, replace
from itertools import combinations
from uuid import UUID, uuid4

import pytest
from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import CasdoorConfiguration, RoleRef, WorkspaceRoleMapping
from core.casdoor.errors import CasdoorDecisionReason, CasdoorErrorCode
from core.casdoor.mapping import (
    BuiltinResolution,
    MappingError,
    MappingIdentityContext,
    ServerWorkspaceAvailability,
    WorkspaceAvailability,
    WorkspaceState,
    resolve_workspace_plan,
)
from core.casdoor.role_graph import EffectiveRoleSnapshot

ORG = "ExactOrg"
DEFAULT = UUID(int=1)
SECOND = UUID(int=2)
ROLES = ("admin", "editor", "normal")


def ref(name):
    return RoleRef(organization=ORG, name=name)


def configuration(mappings=None, default=DEFAULT):
    return CasdoorConfiguration(
        browser_frontend_url="https://idp.example.test",
        backend_api_url="https://idp.example.test",
        expected_issuer="https://issuer.example.test",
        organization=ORG,
        application="ExactApp",
        client_id="ExactClient",
        default_workspace_id=default,
        workspace_mappings=tuple(mappings or ()),
    )


def context():
    return MappingIdentityContext(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "a" * 64,
        "https://issuer.example.test",
        ORG,
        "ExactApp",
        "ExactClient",
        "exact-sub",
    )


def snapshot(names=()):
    return EffectiveRoleSnapshot("exact-sub", StructuredUserRef(ORG, "directory-name"), tuple(ref(n) for n in names))


def availability(ids=(DEFAULT, SECOND)):
    return ServerWorkspaceAvailability(
        tuple(
            WorkspaceAvailability(i, WorkspaceState.NORMAL, tuple(BuiltinResolution(r, f"{i}:{r}") for r in ROLES))
            for i in ids
        )
    )


def resolve(names=(), *, config=None, snap=None, trusted=None, available=None):
    config = config or configuration([WorkspaceRoleMapping(workspace_id=DEFAULT, **{r: ref(r) for r in ROLES})])
    return resolve_workspace_plan(
        configuration=config,
        snapshot=snapshot(names) if snap is None else snap,
        context=context() if trusted is None else trusted,
        availability=availability((DEFAULT,)) if available is None else available,
    )


@pytest.mark.parametrize("names", [c for n in range(4) for c in combinations(ROLES, n)])
def test_every_precedence_combination(names):
    target = resolve(names).targets[0]
    expected = next((r for r in ROLES if r in names), "normal")
    assert target.target_role == expected
    assert target.builtin_id == f"{DEFAULT}:{expected}"
    assert target.matched_role_refs == tuple(ref(r) for r in ROLES if r in names)
    assert target.reason == (
        CasdoorDecisionReason.ROLE_MAPPING if names else CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
    )


def test_multispace_independent_and_shared_ref_mappings():
    config = configuration(
        [
            WorkspaceRoleMapping(workspace_id=SECOND, editor=ref("shared")),
            WorkspaceRoleMapping(workspace_id=DEFAULT, admin=ref("shared"), editor=ref("default-editor")),
        ]
    )
    plan = resolve(("shared", "default-editor"), config=config, available=availability())
    assert [(t.workspace_id, t.target_role) for t in plan.targets] == [(DEFAULT, "admin"), (SECOND, "editor")]
    unmatched = resolve(("unconfigured",), config=config, available=availability())
    assert [(t.workspace_id, t.target_role, t.reason) for t in unmatched.targets] == [
        (DEFAULT, "normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
    ]


@pytest.mark.parametrize("names", [(), ("no-match",)])
def test_known_empty_and_complete_unmatched_have_fixed_fallback(names):
    plan = resolve(names, config=configuration(), available=availability((DEFAULT,)))
    assert len(plan.targets) == 1
    assert plan.targets[0].target_role == "normal"
    assert plan.targets[0].reason == CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK


@pytest.mark.parametrize("name", ["Admin", " admin", "admin ", "org/admin", "renamed-admin"])
def test_exact_names_are_not_split_folded_trimmed_or_rename_migrated(name):
    assert resolve((name,)).targets[0].reason == CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK


def test_structured_slash_name_matches_exactly():
    config = configuration([WorkspaceRoleMapping(workspace_id=DEFAULT, admin=ref("name/with/slash"))])
    assert resolve(("name/with/slash",), config=config).targets[0].target_role == "admin"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        (),
        "admin",
        EffectiveRoleSnapshot("other-sub", StructuredUserRef(ORG, "u"), ()),
        EffectiveRoleSnapshot("exact-sub", StructuredUserRef("OtherOrg", "u"), ()),
        EffectiveRoleSnapshot(
            "exact-sub", StructuredUserRef(ORG, "u"), (RoleRef(organization="OtherOrg", name="admin"),)
        ),
        EffectiveRoleSnapshot("exact-sub", StructuredUserRef(ORG, "u"), [ref("admin")]),
        EffectiveRoleSnapshot("exact-sub", StructuredUserRef(ORG, "u"), ("admin",)),
        EffectiveRoleSnapshot("exact-sub", StructuredUserRef(ORG, "u"), (ref("admin"), ref("admin"))),
        EffectiveRoleSnapshot("exact-sub", StructuredUserRef(ORG, ""), ()),
    ],
)
def test_unknown_malformed_or_cross_org_never_falls_back(bad):
    with pytest.raises(MappingError) as caught:
        resolve_workspace_plan(
            configuration=configuration(), snapshot=bad, context=context(), availability=availability()
        )
    assert caught.value.code == CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert str(caught.value) == "role_snapshot_unknown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://wrong.example.test"),
        ("organization", "exactorg"),
        ("application", "wrong"),
        ("client_id", "wrong"),
        ("namespace_id", "payload-id"),
        ("subject", ""),
        ("config_digest", "BAD"),
    ],
)
def test_missing_or_mismatching_trusted_context_rejected(field, value):
    with pytest.raises(MappingError):
        resolve(trusted=replace(context(), **{field: value}))


def test_context_missing_rejected():
    with pytest.raises(MappingError):
        resolve_workspace_plan(
            configuration=configuration(), snapshot=snapshot(), context=None, availability=availability()
        )


@pytest.mark.parametrize("state", [WorkspaceState.UNKNOWN, WorkspaceState.UNAVAILABLE, "normal"])
def test_unavailable_or_untyped_state_cannot_be_fallback(state):
    available = availability((DEFAULT,))
    available = replace(available, workspaces=(replace(available.workspaces[0], state=state),))
    with pytest.raises(MappingError) as caught:
        resolve(available=available)
    assert caught.value.code == CasdoorErrorCode.WORKSPACE_UNAVAILABLE


@pytest.mark.parametrize(
    "builtins",
    [
        (),
        (BuiltinResolution("normal", "normal-id"),),
        (BuiltinResolution("owner", "owner-id"),),
        (BuiltinResolution("admin", ""),),
        (BuiltinResolution("normal", "same"), BuiltinResolution("admin", "same")),
        (BuiltinResolution("normal", "n"), BuiltinResolution("normal", "n2")),
        None,
    ],
)
def test_missing_unknown_or_ambiguous_builtin_rejected_even_if_not_matched(builtins):
    available = ServerWorkspaceAvailability((WorkspaceAvailability(DEFAULT, WorkspaceState.NORMAL, builtins),))
    with pytest.raises(MappingError) as caught:
        resolve(available=available)
    assert caught.value.code == CasdoorErrorCode.AUTHORIZATION_PENDING


def test_unmatched_secondary_workspace_still_requires_availability():
    config = configuration([WorkspaceRoleMapping(workspace_id=SECOND, admin=ref("secondary"))])
    with pytest.raises(MappingError, match="workspace_unavailable"):
        resolve(config=config)
    available = availability()
    with pytest.raises(MappingError, match="workspace_unavailable"):
        resolve(
            config=config, available=replace(available, workspaces=(available.workspaces[0], available.workspaces[0]))
        )


def test_100_spaces_are_all_checked_and_returned_without_truncation():
    ids = tuple(UUID(int=i) for i in range(1, 101))
    config = configuration([WorkspaceRoleMapping(workspace_id=i, normal=ref("shared")) for i in ids])
    assert len(resolve(("shared",), config=config, available=availability(ids)).targets) == 100
    forged = config.model_copy(
        update={
            "workspace_mappings": config.workspace_mappings
            + (WorkspaceRoleMapping(workspace_id=UUID(int=101), admin=ref("shared")),)
        }
    )
    with pytest.raises(MappingError):
        resolve(("shared",), config=forged, available=availability(ids + (UUID(int=101),)))


def test_forged_configuration_does_not_escape_exact_slots():
    malformed = WorkspaceRoleMapping.model_construct(workspace_id=DEFAULT, admin=ref("same"), editor=ref("same"))
    for mappings in ((malformed,), (WorkspaceRoleMapping(workspace_id=DEFAULT),) * 2):
        with pytest.raises(MappingError):
            resolve(config=configuration().model_copy(update={"workspace_mappings": mappings}))


@pytest.mark.parametrize("invalid", ["\ud800", "bad\nname", "", "x" * 256])
def test_malformed_internal_role_projection_rejected(invalid):
    forged = RoleRef.model_construct(organization=ORG, name=invalid)
    with pytest.raises(MappingError):
        resolve(snap=replace(snapshot(), effective_roles=(forged,)))


def test_role_projection_size_limit_rejects_instead_of_truncating():
    roles = tuple(ref(f"r{i}") for i in range(2001))
    with pytest.raises(MappingError):
        resolve(snap=replace(snapshot(), effective_roles=roles))


def test_partial_internal_pydantic_projection_rejected_with_stable_error():
    with pytest.raises(MappingError, match="role_snapshot_unknown"):
        resolve(snap=replace(snapshot(), effective_roles=(RoleRef.model_construct(organization=ORG),)))
    with pytest.raises(MappingError, match="role_snapshot_unknown"):
        resolve(config=CasdoorConfiguration.model_construct())
    with pytest.raises(MappingError, match="role_snapshot_unknown"):
        resolve(
            config=configuration().model_copy(update={"workspace_mappings": (WorkspaceRoleMapping.model_construct(),)})
        )


def test_plan_is_minimal_frozen_and_does_not_mutate_inputs():
    config, snap, trusted, available = configuration(), snapshot(), context(), availability((DEFAULT,))
    before = (config.model_dump(), snap, trusted, available)
    plan = resolve(config=config, snap=snap, trusted=trusted, available=available)
    assert (config.model_dump(), snap, trusted, available) == before
    assert plan.context == trusted
    assert "exact-sub" not in repr(plan)
    with pytest.raises(FrozenInstanceError):
        plan.targets = ()
    with pytest.raises(FrozenInstanceError):
        plan.targets[0].target_role = "admin"
