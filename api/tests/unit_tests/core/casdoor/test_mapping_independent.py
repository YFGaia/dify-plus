"""Independent adversarial checks for the I11-A mapping boundary.

All IDs and backend availability projections here are synthetic. Passing these
tests says nothing about a deployed Casdoor release or actual RBAC builtin tags.
"""

from dataclasses import replace
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

ORG = "Exact-Org"
DEFAULT = UUID("00000000-0000-0000-0000-000000000011")
OTHER = UUID("00000000-0000-0000-0000-000000000022")
ISSUER = "https://issuer.synthetic.invalid"


def _context() -> MappingIdentityContext:
    return MappingIdentityContext(
        integration_id=UUID("00000000-0000-0000-0000-000000000101"),
        revision_id=UUID("00000000-0000-0000-0000-000000000102"),
        namespace_id=UUID("00000000-0000-0000-0000-000000000103"),
        identity_id=UUID("00000000-0000-0000-0000-000000000104"),
        account_id=UUID("00000000-0000-0000-0000-000000000105"),
        config_digest="a" * 64,
        issuer=ISSUER,
        organization=ORG,
        application="Exact-App",
        client_id="Exact-Client",
        subject="raw/subject:CaseSensitive",
    )


def _config(mappings=()):
    return CasdoorConfiguration(
        browser_frontend_url="https://login.synthetic.invalid",
        backend_api_url="https://api.synthetic.invalid",
        expected_issuer=ISSUER,
        organization=ORG,
        application="Exact-App",
        client_id="Exact-Client",
        default_workspace_id=DEFAULT,
        workspace_mappings=tuple(mappings),
    )


def _availability(workspace_ids, roles_by_workspace=None):
    roles_by_workspace = roles_by_workspace or {}
    return ServerWorkspaceAvailability(
        tuple(
            WorkspaceAvailability(
                workspace_id,
                WorkspaceState.NORMAL,
                tuple(
                    BuiltinResolution(role, f"synthetic:{workspace_id}:{role}")
                    for role in roles_by_workspace.get(workspace_id, ("admin", "editor", "normal"))
                ),
            )
            for workspace_id in workspace_ids
        )
    )


def _snapshot(names=(), *, organization=ORG, subject=None):
    return EffectiveRoleSnapshot(
        _context().subject if subject is None else subject,
        StructuredUserRef(organization, "directory-user"),
        tuple(RoleRef(organization=organization, name=name) for name in names),
    )


def _resolve(names=(), *, config=None, snapshot=None, context=None, availability=None):
    return resolve_workspace_plan(
        configuration=_config() if config is None else config,
        snapshot=_snapshot(names) if snapshot is None else snapshot,
        context=_context() if context is None else context,
        availability=_availability((DEFAULT,)) if availability is None else availability,
    )


def test_known_empty_and_exact_unmatched_roles_use_only_default_normal():
    mapping = WorkspaceRoleMapping(
        workspace_id=DEFAULT,
        admin=RoleRef(organization=ORG, name="ops/admin"),
    )
    for roles in ((), ("Ops/Admin",), ("ops/admin ",), ("other-org/ops/admin",)):
        plan = _resolve(roles, config=_config((mapping,)))
        assert [(item.workspace_id, item.target_role, item.reason) for item in plan.targets] == [
            (DEFAULT, "normal", CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK)
        ]


def test_same_basename_in_another_org_is_unknown_not_a_role_match():
    config = _config((WorkspaceRoleMapping(workspace_id=DEFAULT, editor=RoleRef(organization=ORG, name="reviewer")),))
    other_org_snapshot = _snapshot(("reviewer",), organization="Other-Org")
    with pytest.raises(MappingError) as caught:
        _resolve(config=config, snapshot=other_org_snapshot)
    assert caught.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN


def test_workspace_roles_are_independent_and_precedence_keeps_all_exact_matches():
    alpha, beta = UUID(int=40), UUID(int=30)
    shared = RoleRef(organization=ORG, name="same/name")
    config = _config(
        (
            WorkspaceRoleMapping(workspace_id=alpha, admin=shared, editor=RoleRef(organization=ORG, name="edit-a")),
            WorkspaceRoleMapping(workspace_id=beta, editor=shared, normal=RoleRef(organization=ORG, name="normal-b")),
        )
    )
    plan = _resolve(
        ("same/name", "edit-a", "normal-b"),
        config=config,
        availability=_availability((DEFAULT, alpha, beta)),
    )
    assert [(target.workspace_id, target.target_role) for target in plan.targets] == [
        (DEFAULT, "normal"),
        (beta, "editor"),
        (alpha, "admin"),
    ]
    assert plan.targets[1].matched_role_refs == (shared, RoleRef(organization=ORG, name="normal-b"))
    assert plan.targets[2].matched_role_refs == (shared, RoleRef(organization=ORG, name="edit-a"))


@pytest.mark.parametrize(
    "field",
    [
        "issuer",
        "organization",
        "application",
        "client_id",
        "subject",
    ],
)
def test_each_mapping_context_component_is_required_as_an_exact_value(field):
    original = _context()
    changed = "changed"
    with pytest.raises(MappingError):
        _resolve(context=replace(original, **{field: changed}))


def test_unmatched_secondary_space_still_needs_normal_workspace_and_builtin_projection():
    config = _config((WorkspaceRoleMapping(workspace_id=OTHER, normal=RoleRef(organization=ORG, name="never")),))
    with pytest.raises(MappingError) as unavailable:
        _resolve(config=config)
    assert unavailable.value.code is CasdoorErrorCode.WORKSPACE_UNAVAILABLE

    partial = _availability((DEFAULT, OTHER), {OTHER: ("admin", "editor")})
    with pytest.raises(MappingError) as missing_normal:
        _resolve(config=config, availability=partial)
    assert missing_normal.value.code is CasdoorErrorCode.AUTHORIZATION_PENDING


def test_unknown_role_snapshot_and_unknown_workspace_state_cannot_become_fallback():
    malformed_snapshot = replace(_snapshot(), effective_roles=("apparently-empty",))
    with pytest.raises(MappingError) as unknown_roles:
        _resolve(snapshot=malformed_snapshot)
    assert unknown_roles.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN

    available = _availability((DEFAULT,))
    unavailable = replace(available.workspaces[0], state=WorkspaceState.UNKNOWN)
    with pytest.raises(MappingError) as unknown_workspace:
        _resolve(availability=replace(available, workspaces=(unavailable,)))
    assert unknown_workspace.value.code is CasdoorErrorCode.WORKSPACE_UNAVAILABLE


def test_string_builtin_ids_are_only_synthetic_projection_values():
    """Mapping consumes typed input; it cannot establish real Owner/builtin proof."""
    availability = _availability((DEFAULT,))
    plan = _resolve(availability=availability)
    assert plan.targets[0].builtin_id == f"synthetic:{DEFAULT}:normal"
    assert isinstance(availability.workspaces[0].builtins[0], BuiltinResolution)


def test_valid_shaped_ids_and_digest_do_not_prove_database_provenance():
    """UUIDs/digest are opaque here; repository must bind them to current rows."""
    original = _context()
    altered = replace(
        original,
        integration_id=uuid4(),
        revision_id=uuid4(),
        namespace_id=uuid4(),
        identity_id=uuid4(),
        account_id=uuid4(),
        config_digest="f" * 64,
    )
    result = _resolve(context=altered)
    assert result.context == altered

    with pytest.raises(MappingError):
        _resolve(context=replace(original, config_digest="F" * 64))
