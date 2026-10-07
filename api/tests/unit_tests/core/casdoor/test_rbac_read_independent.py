"""Independent counterexamples for raw RBAC parsing and candidate boundaries."""

import json
from dataclasses import replace
from uuid import UUID

import pytest
from core.casdoor import rbac_read
from core.casdoor.ownership import ExternalMemberRolesProjection, MemberRole

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT = UUID("22222222-2222-4222-8222-222222222222")
CONTRACT = rbac_read.RawRbacContract.OFFLINE_FIXTURE_V1


def _role(**changes):
    return {
        "id": "role-1",
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Display only",
        "category": "global_system_default",
        "role_tag": "owner",
        "is_builtin": True,
        "permission_keys": [],
        **changes,
    }


def _page(role):
    return {
        "data": [role],
        "pagination": {"total_count": 1, "per_page": 1, "current_page": 1, "total_pages": 1},
    }


def _raw(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_actual_enterprise_models_can_hide_raw_defaulting_and_coercion():
    from services.enterprise.rbac_service import Paginated, RBACRole

    # The real response model accepts a lossy Owner-looking role and silently
    # supplies the authorization fields that a raw candidate must observe.
    provider_item = {"id": "r", "tenant_id": str(WORKSPACE), "type": "workspace", "name": "Owner"}
    provider_page = {
        "data": [provider_item],
        "pagination": {"total_count": False, "per_page": 1, "current_page": 1, "total_pages": 1},
        "workspace_id": str(WORKSPACE),
    }
    hydrated = Paginated[RBACRole].model_validate(provider_page)
    hydrated_role = hydrated.data[0]
    assert hydrated_role.is_builtin is False
    assert hydrated_role.role_tag == hydrated_role.category == ""
    assert hydrated_role.permission_keys == [] and hydrated_role.tenant_id == str(WORKSPACE)
    assert "is_builtin" not in hydrated_role.model_fields_set
    assert hydrated.pagination is not None and hydrated.pagination.total_count == 0
    assert "workspace_id" not in hydrated.model_fields_set

    with pytest.raises(rbac_read.RbacCandidateError):
        rbac_read.decode_catalog_candidate((_raw(provider_page),), workspace_id=WORKSPACE, contract=CONTRACT)


def test_json_bool_pagination_that_pydantic_accepts_is_not_an_integer_candidate():
    from services.enterprise.rbac_service import Pagination

    pagination = {"total_count": True, "per_page": 1, "current_page": 1, "total_pages": 1}
    assert Pagination.model_validate(pagination).total_count == 1
    body = _raw({"data": [_role()], "pagination": pagination})
    with pytest.raises(rbac_read.RbacCandidateError):
        rbac_read.decode_catalog_candidate((body,), workspace_id=WORKSPACE, contract=CONTRACT)


def test_nested_duplicate_pagination_key_is_rejected_even_when_equal():
    raw = _raw(_page(_role())).replace(b'"total_count":1', b'"total_count":1,"total_count":1', 1)
    with pytest.raises(rbac_read.RbacCandidateError):
        rbac_read.decode_catalog_candidate((raw,), workspace_id=WORKSPACE, contract=CONTRACT)


def test_owner_label_does_not_classify_custom_role_and_owner_is_not_a_desired_target():
    from core.casdoor.ownership import has_remote_owner

    custom = _role(id="custom-owner", category="global_custom", is_builtin=False)
    catalog = rbac_read.decode_catalog_candidate((_raw(_page(custom)),), workspace_id=WORKSPACE, contract=CONTRACT)
    assert not has_remote_owner(catalog.roles)
    with pytest.raises(rbac_read.RbacCandidateError):
        rbac_read.desired_builtin_candidate(catalog, "owner")


def test_malformed_publicly_constructed_candidate_does_not_become_desired_role():
    catalog = rbac_read.decode_catalog_candidate(
        (_raw(_page(_role(role_tag="admin"))),), workspace_id=WORKSPACE, contract=CONTRACT
    )
    malformed_role = MemberRole(
        "forged",
        True,
        "global_system_default",
        "admin",
        permission_keys=["permission"],  # type: ignore[arg-type]
    )
    malformed = replace(catalog, roles=(malformed_role,), total_count=1)
    with pytest.raises(rbac_read.RbacCandidateError):
        rbac_read.desired_builtin_candidate(malformed, "admin")

    # Even a normal decoder result is only an offline value object; its shape
    # does not cross the future authenticated projection boundary.
    assert not isinstance(catalog, ExternalMemberRolesProjection)
    assert not hasattr(catalog, "knowledge")


def test_release_like_selector_text_never_activates_before_raw_parse():
    from unittest.mock import patch

    raw = b'{"data":[],"pagination":{}}'
    for selector in (None, "offline_fixture_v1", "release-1"):
        with patch.object(rbac_read.json, "loads", side_effect=AssertionError("parsed before selector")):
            with pytest.raises(rbac_read.RbacCandidateError):
                rbac_read.decode_catalog_candidate((raw,), workspace_id=WORKSPACE, contract=selector)
