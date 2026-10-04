"""Independent hostile-input checks for the offline resource-config candidate parser."""

import json
from uuid import UUID

import pytest
from core.casdoor import resource_config_read as read
from services.enterprise.rbac_service import RBACResourceType as Kind

CONTRACT = read.ResourceConfigContract.OFFLINE_FIXTURE_V1
W = UUID(int=71)
C = UUID(int=72)
APP = (Kind.APP, UUID(int=1))
DATASET = (Kind.DATASET, UUID(int=1))
AGENT = (Kind.AGENT, UUID(int=2))


def make_request(*, selected=(APP, DATASET, AGENT)):
    return read.build_resource_config_request(
        contract=CONTRACT,
        workspace_id=W,
        context_id=C,
        inventory=(APP, DATASET, AGENT),
        selected=selected,
    )


def make_row(pair=APP, **extra):
    return {
        "resource_type": pair[0].value,
        "resource_id": str(pair[1]),
        "automatic_include_workspace_members": True,
        **extra,
    }


def encode(rows):
    return json.dumps({"data": rows}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def assert_safe_rejection(raw, request=None):
    with pytest.raises(read.ResourceConfigCandidateError) as error:
        read.parse_resource_config(raw, request=request or make_request())
    assert str(error.value) == "resource_config_candidate_invalid"


def test_true_prefix_followed_by_invalid_unicode_metadata_is_atomic_and_safe():
    raw = (
        b'{"data":[{"resource_type":"app","resource_id":"00000000-0000-0000-0000-000000000001",'
        b'"automatic_include_workspace_members":true},'
        b'{"resource_type":"dataset","resource_id":"00000000-0000-0000-0000-000000000001",'
        b'"automatic_include_workspace_members":true,"scope":"\\ud800"}]}'
    )
    assert_safe_rejection(raw)


def test_bracket_and_quote_characters_inside_strings_do_not_hide_excess_depth():
    deep = b"[" * (read.MAX_DEPTH + 1) + b'{"s":"escaped \\" ] ["}' + b"]" * (read.MAX_DEPTH + 1)
    assert_safe_rejection(deep)

    # Structural brackets inside a JSON string are ignored by the preflight scanner.
    ordinary = encode([make_row(scope='literal \\" [{]} and escaped quote')])
    parsed = read.parse_resource_config(ordinary, request=make_request(selected=(APP,)))
    assert parsed.observations == ((APP, read.ResourceConfigObservation.RETURNED_TRUE),)


def test_same_uuid_different_kinds_subset_order_and_omission_are_preserved():
    request = make_request(selected=(AGENT, DATASET))
    assert request.pairs == (DATASET, AGENT)
    result = read.parse_resource_config(encode([make_row(AGENT)]), request=request)
    assert result.observations == (
        (DATASET, read.ResourceConfigObservation.OMITTED_UNKNOWN),
        (AGENT, read.ResourceConfigObservation.RETURNED_TRUE),
    )


def test_response_for_unselected_kind_is_rejected_even_with_matching_uuid():
    request = make_request(selected=(APP,))
    assert_safe_rejection(encode([make_row(DATASET)]), request)


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_raw_toggle_requires_boolean_and_never_uses_dto_default(value):
    changed = make_row()
    changed["automatic_include_workspace_members"] = value
    assert_safe_rejection(encode([changed]))


def test_missing_toggle_after_true_prefix_does_not_return_partial_observations():
    incomplete = make_row(DATASET)
    del incomplete["automatic_include_workspace_members"]
    assert_safe_rejection(encode([make_row(APP), incomplete]))


def test_candidate_is_only_fixture_context_and_exposes_no_runtime_or_acl_authority():
    request = make_request(selected=(APP,))
    assert request.contract is CONTRACT
    assert request.workspace_id == W and request.context_id == C
    assert not ({"reader", "runtime", "complete", "known_absence", "acl", "grant"} & set(dir(request)))
    observation = read.parse_resource_config(encode([make_row()]), request=request).observations[0][1]
    assert observation is read.ResourceConfigObservation.RETURNED_TRUE
    assert observation not in (
        read.ResourceConfigObservation.OMITTED_UNKNOWN,
        "complete",
        "known_absence",
        "grant",
    )


def test_explicit_empty_data_and_missing_selection_remain_unknown_not_default_false():
    result = read.parse_resource_config(b'{"data":[]}', request=make_request(selected=(APP,)))
    assert result.observations == ((APP, read.ResourceConfigObservation.OMITTED_UNKNOWN),)
    assert result.observations[0][1] is not read.ResourceConfigObservation.RETURNED_FALSE
