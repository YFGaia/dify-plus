"""Offline raw bytes only: candidates never attest real release/auth/freshness."""

import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from core.casdoor import rbac_read as read
from core.casdoor.ownership import ExternalMemberRolesProjection, has_remote_owner
from models.account import TenantAccountRole

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT = UUID("22222222-2222-4222-8222-222222222222")
CONTRACT = read.RawRbacContract.OFFLINE_FIXTURE_V1


def encoded(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def role(identifier="r1", **changes):
    return {
        "id": identifier,
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Display name",
        "category": "",
        "role_tag": "",
        "is_builtin": False,
        "permission_keys": [],
        **changes,
    }


def member(roles=None, **changes):
    return {"account_id": str(ACCOUNT), "roles": [] if roles is None else roles, **changes}


def decode(value, **changes):
    return read.decode_member_candidate(
        encoded(value), **{"workspace_id": WORKSPACE, "account_id": ACCOUNT, "contract": CONTRACT, **changes}
    )


def page(roles, total=None, per_page=2, current=1, count=1):
    return {
        "data": roles,
        "pagination": {
            "total_count": len(roles) if total is None else total,
            "per_page": per_page,
            "current_page": current,
            "total_pages": count,
        },
    }


def catalog(*values, **changes):
    return read.decode_catalog_candidate(
        tuple(encoded(value) for value in values), **{"workspace_id": WORKSPACE, "contract": CONTRACT, **changes}
    )


def test_actual_dto_missing_defaults_and_null_coercion_are_not_raw_evidence():
    from services.enterprise.rbac_service import MemberRolesResponse, Paginated, RBACRole

    missing = MemberRolesResponse.model_validate({"account_id": str(ACCOUNT)})
    assert missing.roles == [] and "roles" not in missing.model_fields_set
    with pytest.raises(read.RbacCandidateError):
        decode({"account_id": str(ACCOUNT)})
    minimal = {"id": "r1", "type": "workspace", "name": "Display name"}
    hydrated = RBACRole.model_validate(minimal)
    assert hydrated.category == hydrated.role_tag == ""
    assert hydrated.is_builtin is False and hydrated.permission_keys == [] and hydrated.tenant_id is None
    assert not {"category", "role_tag", "is_builtin", "permission_keys", "tenant_id"} & hydrated.model_fields_set
    with pytest.raises(read.RbacCandidateError):
        decode(member([minimal]))
    assert RBACRole.model_validate({**minimal, "permission_keys": None}).permission_keys == []
    with pytest.raises(read.RbacCandidateError):
        decode(member([role(permission_keys=None)]))
    default_catalog = Paginated[RBACRole].model_validate({})
    assert default_catalog.data == [] and default_catalog.pagination is None
    with pytest.raises(read.RbacCandidateError):
        catalog({})


@pytest.mark.parametrize("contract", [None, False, True, "offline_fixture_v1", "release_unproved"])
def test_missing_unsupported_or_payload_contract_never_enables_candidate(contract):
    with pytest.raises(read.RbacCandidateError):
        decode(member(), contract=contract)
    with pytest.raises(read.RbacCandidateError):
        catalog(page([]), contract=contract)


def test_explicit_empty_member_and_catalog_are_candidates_not_trusted_projections():
    out = decode(member())
    empty = catalog(page([]))
    assert out.roles == empty.roles == ()
    assert out.workspace_id == WORKSPACE and out.account_id == ACCOUNT
    assert empty.total_count == 0 and empty.page_count == 1
    assert not isinstance(out, ExternalMemberRolesProjection)
    assert not hasattr(out, "knowledge")
    # Empty responses carry no raw workspace scope; B2 must authenticate it.
    assert decode(member(), workspace_id=uuid4()).roles == ()


def test_custom_roles_and_explicit_empty_metadata_preserve_all_permission_fields():
    out = decode(member([role("z", permission_keys=["write", "read"]), role("a", description="")]))
    assert [item.role_id for item in out.roles] == ["a", "z"]
    assert out.roles[1].permission_keys == ("write", "read")
    assert not has_remote_owner(out.roles)


@pytest.mark.parametrize(
    "builtin,category,tag,display,owner",
    [
        (True, "global_system_default", "owner", "Not Owner", True),
        (False, "global_system_default", "owner", "Owner", False),
        (True, "custom", "owner", "Owner", False),
        (True, "global_system_default", "OWNER", "Owner", False),
        (False, "", "", "Owner", False),
    ],
)
def test_owner_classification_reuses_exact_authorization_owner(builtin, category, tag, display, owner):
    out = decode(member([role(is_builtin=builtin, category=category, role_tag=tag, name=display)]))
    assert has_remote_owner(out.roles) is owner


@pytest.mark.parametrize("field", list(role()))
def test_every_owner_or_shape_field_must_be_present(field):
    value = role()
    del value[field]
    with pytest.raises(read.RbacCandidateError):
        decode(member([value]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", 12),
        ("id", ""),
        ("id", "bad\n"),
        ("tenant_id", None),
        ("tenant_id", str(uuid4())),
        ("tenant_id", 1),
        ("type", None),
        ("type", "app"),
        ("name", None),
        ("name", ""),
        ("name", 1),
        ("category", None),
        ("category", 1),
        ("role_tag", None),
        ("role_tag", 1),
        ("is_builtin", None),
        ("is_builtin", 1),
        ("is_builtin", "true"),
        ("permission_keys", None),
        ("permission_keys", "read"),
        ("permission_keys", {}),
        ("permission_keys", ["read", "read"]),
        ("permission_keys", [1]),
        ("permission_keys", [""]),
        ("permission_keys", ["bad\x00"]),
        ("permission_keys", [["read"]]),
        ("description", None),
        ("description", "x" * 2049),
        ("unexpected", True),
    ],
)
def test_raw_null_coercion_control_and_unknown_fields_fail_closed(field, value):
    with pytest.raises(read.RbacCandidateError, match="^authorization_pending$"):
        decode(member([role(**{field: value})]))


@pytest.mark.parametrize("field", ["account_id", "roles"])
def test_member_required_envelope_fields(field):
    value = member()
    del value[field]
    with pytest.raises(read.RbacCandidateError):
        decode(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": str(uuid4())},
        {"account_id": None},
        {"roles": None},
        {"roles": {}},
        {"workspace_id": str(WORKSPACE)},
        {"complete": True},
    ],
)
def test_scope_missing_or_unproven_shape_is_not_a_member_set(changes):
    value = member()
    value.update(changes)
    with pytest.raises(read.RbacCandidateError):
        decode(value)


@pytest.mark.parametrize("field", ["workspace_id", "account_id"])
def test_scope_arguments_must_be_uuid_objects(field):
    with pytest.raises(read.RbacCandidateError):
        decode(member(), **{field: str(WORKSPACE)})


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"roles":[],"roles":[]}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1e999}',
        b'{"a":1.0}',
        b'{"a":{"b":1,"b":2}}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_malformed_utf8_duplicate_keys_nonfinite_and_deep_json_are_pending(raw):
    with pytest.raises(read.RbacCandidateError):
        read.decode_member_candidate(raw, workspace_id=WORKSPACE, account_id=ACCOUNT, contract=CONTRACT)


def test_nested_role_duplicate_field_is_rejected_even_when_values_are_equal():
    raw = encoded(member([role()])).replace(b'"is_builtin":false', b'"is_builtin":false,"is_builtin":false')
    with pytest.raises(read.RbacCandidateError):
        read.decode_member_candidate(raw, workspace_id=WORKSPACE, account_id=ACCOUNT, contract=CONTRACT)


@pytest.mark.parametrize("raw", ["{}", bytearray(b"{}"), memoryview(b"{}"), b" " * (read.MAX_MEMBER_BYTES + 1)])
def test_member_byte_bound_precedes_json_loads(raw):
    with patch.object(read.json, "loads", side_effect=AssertionError("must not parse")):
        with pytest.raises(read.RbacCandidateError):
            read.decode_member_candidate(raw, workspace_id=WORKSPACE, account_id=ACCOUNT, contract=CONTRACT)


def test_duplicate_role_ids_are_not_collapsed():
    with pytest.raises(read.RbacCandidateError):
        decode(member([role(), role(is_builtin=True)]))


@pytest.mark.parametrize(
    "roles",
    [
        [role(str(i)) for i in range(read.MAX_MEMBER_ROLES + 1)],
        [role(permission_keys=["p"] * (read.MAX_PERMISSION_KEYS + 1))],
        [role("中" * 700)],
        [role(category="x" * 2049)],
        [role(role_tag="x" * 2049)],
        [role(permission_keys=["x" * 2049])],
        [role(name="中" * 700)],
        [role(permission_keys=[f"p{i}" + "x" * 120 for i in range(500)])],
        [
            role("a", permission_keys=[f"p{i}" for i in range(4097)]),
            role("b", permission_keys=[f"q{i}" for i in range(4097)]),
        ],
    ],
)
def test_role_permission_scalar_and_canonical_byte_budgets(roles):
    with pytest.raises(read.RbacCandidateError):
        decode(member(roles))


def test_exact_scalar_boundary_is_preserved_without_truncation():
    out = decode(member([role("x" * 2048, permission_keys=["p" * 2048], name="n" * 2048)]))
    assert out.roles[0].role_id == "x" * 2048 and out.roles[0].permission_keys == ("p" * 2048,)


def test_full_catalog_requires_last_page_and_canonicalizes_across_pages():
    first = page([role("z"), role("a")], total=3, count=2)
    last = page([role("m")], total=3, current=2, count=2)
    out = catalog(first, last)
    assert [item.role_id for item in out.roles] == ["a", "m", "z"]
    assert out.page_count == 2 and out.total_count == 3
    with pytest.raises(read.RbacCandidateError):
        catalog(first)
    with pytest.raises(read.RbacCandidateError):
        catalog(last, first)


@pytest.mark.parametrize("field", ["data", "pagination"])
def test_catalog_envelope_presence(field):
    value = page([])
    del value[field]
    with pytest.raises(read.RbacCandidateError):
        catalog(value)


@pytest.mark.parametrize("field", ["total_count", "per_page", "current_page", "total_pages"])
@pytest.mark.parametrize("value", [None, True, False, "1", 1.0, -1])
def test_pagination_values_never_coerce(field, value):
    envelope = page([])
    envelope["pagination"][field] = value
    with pytest.raises(read.RbacCandidateError):
        catalog(envelope)


@pytest.mark.parametrize("field", ["total_count", "per_page", "current_page", "total_pages"])
def test_all_pagination_metadata_is_required(field):
    value = page([])
    del value["pagination"][field]
    with pytest.raises(read.RbacCandidateError):
        catalog(value)


@pytest.mark.parametrize(
    "values",
    [
        (page([], total=1),),
        (page([role()], total=0),),
        (page([], per_page=0),),
        (page([], per_page=129),),
        (page([], count=0),),
        (page([], total=2049),),
        (page([], count=33),),
        (page([], current=0),),
        (page([role()], total=3, count=2), page([role("b")], total=3, current=2, count=2)),
        (page([role(), role("b")], total=3, count=2), page([role("c")], total=4, current=2, count=2)),
        (page([role(), role("b")], total=3, count=2), page([role("c")], total=3, current=1, count=2)),
        (page([role(), role("b")], total=3, count=2), page([role("c")], total=3, per_page=3, current=2, count=2)),
        (page([role()], total=2, per_page=1, count=2), page([role()], total=2, per_page=1, current=2, count=2)),
        ({"data": [], "pagination": None},),
        ({**page([]), "complete": True},),
    ],
)
def test_partial_inconsistent_duplicate_page_and_cross_page_id_catalogs_are_pending(values):
    with pytest.raises(read.RbacCandidateError):
        catalog(*values)


@pytest.mark.parametrize(
    "pages",
    [
        (),
        [],
        (b"{}",) * 33,
        (b" " * (read.MAX_PAGE_BYTES + 1),),
        (b" " * read.MAX_PAGE_BYTES, b" " * read.MAX_PAGE_BYTES, b"{}"),
    ],
)
def test_catalog_raw_page_and_aggregate_budgets_precede_all_json_parsing(pages):
    with patch.object(read.json, "loads", side_effect=AssertionError("must not parse")):
        with pytest.raises(read.RbacCandidateError):
            read.decode_catalog_candidate(pages, workspace_id=WORKSPACE, contract=CONTRACT)


def test_catalog_canonical_byte_budget_applies_across_pages():
    one = role("a", permission_keys=[f"p{i}" + "x" * 80 for i in range(350)])
    two = role("b", permission_keys=[f"q{i}" + "x" * 80 for i in range(350)])
    with pytest.raises(read.RbacCandidateError):
        catalog(page([one], total=2, per_page=1, count=2), page([two], total=2, per_page=1, current=2, count=2))


def test_permission_count_budget_applies_across_pages():
    one = role("a", permission_keys=[f"p{i}" for i in range(4097)])
    two = role("b", permission_keys=[f"q{i}" for i in range(4097)])
    with pytest.raises(read.RbacCandidateError):
        catalog(page([one], total=2, per_page=1, count=2), page([two], total=2, per_page=1, current=2, count=2))


def test_builtin_after_first_hundred_requires_and_uses_final_page():
    first = page([role(f"custom-{i}") for i in range(100)], total=101, per_page=100, count=2)
    last = page(
        [role("normal-id", is_builtin=True, category="global_system_default", role_tag="normal")],
        total=101,
        per_page=100,
        current=2,
        count=2,
    )
    desired = read.desired_builtin_candidate(catalog(first, last), "normal")
    assert desired.roles[0].role_id == "normal-id"
    with pytest.raises(read.RbacCandidateError):
        catalog(first)


def test_exact_raw_and_aggregate_byte_caps_accept_without_clipping():
    raw = encoded(member())
    raw += b" " * (read.MAX_MEMBER_BYTES - len(raw))
    assert read.decode_member_candidate(raw, workspace_id=WORKSPACE, account_id=ACCOUNT, contract=CONTRACT).roles == ()
    pages = []
    for current in (1, 2):
        body = encoded(page([role(f"r{current}")], total=2, per_page=1, current=current, count=2))
        pages.append(body + b" " * (read.MAX_PAGE_BYTES - len(body)))
    assert sum(map(len, pages)) == read.MAX_CATALOG_BYTES
    assert read.decode_catalog_candidate(tuple(pages), workspace_id=WORKSPACE, contract=CONTRACT).total_count == 2


def test_exact_page_and_per_page_caps_accept_without_first_page_shortcut():
    values = [page([role(f"r{i}")], total=32, per_page=1, current=i, count=32) for i in range(1, 33)]
    assert catalog(*values).page_count == 32
    assert catalog(page([], per_page=128)).total_count == 0


@pytest.mark.parametrize("target", ["admin", "editor", "normal"])
def test_builtin_desired_is_exact_singleton_and_local_join_remains_normal(target):
    values = [
        role(tag, is_builtin=True, category="global_system_default", role_tag=tag)
        for tag in ("owner", "admin", "editor", "normal")
    ]
    out = catalog(page(values[:2], total=4, count=2), page(values[2:], total=4, current=2, count=2))
    desired = read.desired_builtin_candidate(out, target)
    assert desired.target_role == target and desired.roles == (next(r for r in out.roles if r.role_id == target),)
    assert desired.join_role is TenantAccountRole.NORMAL and desired.workspace_id == WORKSPACE
    assert not isinstance(desired, ExternalMemberRolesProjection)


@pytest.mark.parametrize("target", ["owner", "dataset_operator", "custom", "ADMIN", None, True])
def test_no_owner_or_custom_desired_target(target):
    with pytest.raises(read.RbacCandidateError):
        read.desired_builtin_candidate(catalog(page([])), target)


@pytest.mark.parametrize(
    "values",
    [
        [],
        [role(name="Admin", role_tag="admin")],
        [role(is_builtin=True, category="custom", role_tag="admin")],
        [role(is_builtin=True, category="global_system_default", role_tag="ADMIN")],
        [
            role("a", is_builtin=True, category="global_system_default", role_tag="admin"),
            role("b", is_builtin=True, category="global_system_default", role_tag="admin"),
        ],
        [role("x" * 256, is_builtin=True, category="global_system_default", role_tag="admin")],
    ],
)
def test_absent_ambiguous_or_mapping_overbudget_builtin_is_pending_without_fallback(values):
    with pytest.raises(read.RbacCandidateError):
        read.desired_builtin_candidate(catalog(page(values)), "admin")


@pytest.mark.parametrize(
    "changes",
    [
        {"contract": None},
        {"workspace_id": "bad"},
        {"roles": []},
        {"total_count": True},
        {"total_count": 2},
        {"page_count": True},
        {"page_count": 0},
    ],
)
def test_fabricated_candidate_shape_is_not_silently_accepted(changes):
    out = catalog(page([role(is_builtin=True, category="global_system_default", role_tag="normal")]))
    with pytest.raises(read.RbacCandidateError):
        read.desired_builtin_candidate(replace(out, **changes), "normal")


def test_error_never_contains_raw_provider_content():
    with pytest.raises(read.RbacCandidateError) as failure:
        decode(member([role("DO_NOT_DISCLOSE", permission_keys=None)]))
    assert str(failure.value) == "authorization_pending"
    assert failure.value.code.value == "authorization_pending"
