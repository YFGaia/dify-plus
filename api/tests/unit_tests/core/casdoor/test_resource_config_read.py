"""Hostile synthetic raw fixtures; no backend contract or authorization proof."""

import json
from dataclasses import FrozenInstanceError
from unittest.mock import patch
from uuid import UUID

import pytest

from core.casdoor import resource_config_read as read
from services.enterprise.rbac_service import RBACResourceType as Kind

CONTRACT = read.ResourceConfigContract.OFFLINE_FIXTURE_V1
W = UUID(int=9000)
C = UUID(int=9001)
A = (Kind.APP, UUID(int=1))
D = (Kind.DATASET, UUID(int=1))
G = (Kind.AGENT, UUID(int=2))


def request(inventory=(A, D, G), selected=(A, D, G), **kwargs):
    return read.build_resource_config_request(
        contract=CONTRACT, workspace_id=W, context_id=C, inventory=inventory, selected=selected, **kwargs
    )


def row(pair=A, **kwargs):
    return dict(
        resource_type=pair[0].value, resource_id=str(pair[1]), automatic_include_workspace_members=True, **kwargs
    )


def raw(rows):
    return json.dumps({"data": rows}, ensure_ascii=True, separators=(",", ":")).encode()


def parse(data):
    return read.parse_resource_config(data, request=request())


def invalid(data):
    with pytest.raises(read.ResourceConfigCandidateError, match="^resource_config_candidate_invalid$"):
        parse(data)


def test_returned_false_omitted_and_cross_kind_identity_are_distinct():
    false = row(D)
    false["automatic_include_workspace_members"] = False
    result = parse(raw([false, row()]))
    assert result.observations == (
        (A, read.ResourceConfigObservation.RETURNED_TRUE),
        (D, read.ResourceConfigObservation.RETURNED_FALSE),
        (G, read.ResourceConfigObservation.OMITTED_UNKNOWN),
    )
    assert all(
        state is read.ResourceConfigObservation.OMITTED_UNKNOWN for _, state in parse(b'{"data":[]}').observations
    )
    assert result.request.workspace_id == W and result.request.context_id == C
    with pytest.raises(FrozenInstanceError):
        result.request.workspace_id = UUID(int=8)
    with pytest.raises(FrozenInstanceError):
        result.observations = ()


def test_exact_subset_deterministic_original_request_body_and_empty():
    candidate = request(selected=(G, A))
    assert candidate.pairs == (A, G)
    assert candidate.body == (
        b'{"resources":[{"resource_type":"app","resource_id":"00000000-0000-0000-0000-000000000001"},'
        b'{"resource_type":"agent","resource_id":"00000000-0000-0000-0000-000000000002"}]}'
    )
    empty = request(selected=())
    assert empty.body == b'{"resources":[]}'
    assert read.parse_resource_config(b'{"data":[]}', request=empty).observations == ()
    with pytest.raises(read.ResourceConfigCandidateError):
        read.parse_resource_config(raw([row(D)]), request=candidate)


@pytest.mark.parametrize("toggle", [None, 0, 1, "true", "false", [], {}])
def test_toggle_never_coerced(toggle):
    changed = row()
    changed["automatic_include_workspace_members"] = toggle
    invalid(raw([changed]))


def test_missing_toggle_and_partial_success_discarded():
    malformed = row(D)
    del malformed["automatic_include_workspace_members"]
    invalid(raw([row(), malformed]))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"null",
        b"[]",
        b"{}",
        b'{"data":null}',
        b'{"data":{}}',
        b'{"data":[null]}',
        b'{"data":[],"data":[]}',
        b'{"data":[],"cursor":null}',
        b'{"data":[],"pagination":{}}',
        b'{"data":[],"complete":true}',
        b'{"data":[],"extra":1}',
        b'{"data":[]} trailing',
        b"\xff",
        b'\xef\xbb\xbf{"data":[]}',
        b'{"data":[NaN]}',
        b'{"data":[Infinity]}',
        b'{"data":[-Infinity]}',
        b'{"data":[1e999]}',
        b'{"data":[1.0]}',
        b'{"data":[0]}',
    ],
)
def test_hostile_raw_envelopes(data):
    invalid(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_type", "APP"),
        ("resource_type", "role"),
        ("resource_type", None),
        ("resource_id", "00000000000000000000000000000001"),
        ("resource_id", "00000000-0000-0000-0000-00000000000A"),
        ("resource_id", str(UUID(int=999))),
        ("resource_id", None),
        ("extra", "secret"),
    ],
)
def test_foreign_noncanonical_and_unknown_fields(field, value):
    changed = row()
    changed[field] = value
    invalid(raw([changed]))


def test_duplicate_rows_and_raw_keys():
    invalid(raw([row(), row()]))
    invalid(raw([row()]).replace(b'"resource_type":"app"', b'"resource_type":"app","resource_type":"app"'))


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"account_ids": None},
        {"account_ids": []},
        {"account_ids": [str(UUID(int=3))]},
        {"scope": None},
        {"scope": ""},
        {"scope": "workspace"},
        {"rbac_whitelist_scope": "custom"},
        {"scope": "é" * 1024},
        {"scope": '[{\\"}]'},
    ],
)
def test_optional_metadata_is_only_synthetic_validated_metadata(meta):
    assert parse(raw([row(**meta)])).observations[0][1] is read.ResourceConfigObservation.RETURNED_TRUE


@pytest.mark.parametrize(
    "meta",
    [
        {"account_ids": "[]"},
        {"account_ids": [None]},
        {"account_ids": [str(UUID(int=3))] * 2},
        {"account_ids": [str(UUID(int=i)) for i in range(129)]},
        {"scope": "é" * 1025},
        {"scope": "\ud800"},
        {"scope": "\n"},
        {"scope": 1},
        {"scope": {}},
        {"scope": "x", "rbac_whitelist_scope": "x"},
        {"scope": "x", "rbac_whitelist_scope": "y"},
        {"scope": None, "rbac_whitelist_scope": None},
    ],
)
def test_invalid_metadata(meta):
    invalid(raw([row(**meta)]))


def test_body_budget_precedes_json_and_unicode_parsing():
    padded = b'{"data":[]}' + b" " * (read.MAX_RESPONSE_BYTES - 11)
    assert len(padded) == read.MAX_RESPONSE_BYTES
    parse(padded)
    with patch.object(read.json, "loads", side_effect=AssertionError("must reject before decode")):
        invalid(padded + b"\xff")
        invalid(b"[" * 9 + b"]" * 9)
        invalid(b"[" * 10000)
    for data in (bytearray(b'{"data":[]}'), '{"data":[]}'):
        invalid(data)


def test_nesting_boundary_preflight_then_unsupported_shape_rejects():
    # Exactly eight is allowed through structural preflight, but not through schema.
    assert read._preflight(b"[" * 8 + b"]" * 8) == "[" * 8 + "]" * 8
    invalid(b"[" * 8 + b"]" * 8)


@pytest.mark.parametrize("size,ok", [(0, True), (1, True), (500, True), (501, False)])
def test_request_batch_bounds(size, ok):
    pairs = tuple((Kind.APP, UUID(int=i)) for i in range(size))
    if ok:
        assert request(pairs, pairs).pairs == pairs
    else:
        with pytest.raises(read.ResourceConfigCandidateError):
            request(pairs, pairs)


@pytest.mark.parametrize("size,ok", [(4096, True), (4097, False)])
def test_inventory_bounds(size, ok):
    pairs = tuple((Kind.APP, UUID(int=i)) for i in range(size))
    if ok:
        assert request(pairs, pairs[:1]).pairs == pairs[:1]
    else:
        with pytest.raises(read.ResourceConfigCandidateError):
            request(pairs, ())


@pytest.mark.parametrize(
    "inventory,selected",
    [
        ((D, A), (A,)),
        ((A, A), (A,)),
        ((A,), (D,)),
        ((A,), (A, A)),
        ([A], (A,)),
        ((A,), [A]),
        ((("app", A[1]),), (A,)),
        (((Kind.APP, str(A[1])),), (A,)),
        (((Kind.APP,),), ()),
    ],
)
def test_candidate_consistency_rejects_bad_input(inventory, selected):
    with pytest.raises(read.ResourceConfigCandidateError):
        request(inventory, selected)


def test_contract_context_and_workspace_are_explicit():
    for changes in (
        {"contract": None},
        {"contract": "offline_fixture_v1"},
        {"workspace_id": str(W)},
        {"context_id": None},
    ):
        args = dict(contract=CONTRACT, workspace_id=W, context_id=C, inventory=(A,), selected=(A,))
        args.update(changes)
        with pytest.raises(read.ResourceConfigCandidateError):
            read.build_resource_config_request(**args)


def test_encoded_request_budget_checked_with_actual_encoded_bytes():
    length = len(request().body)
    with patch.object(read, "MAX_REQUEST_BYTES", length):
        assert len(request().body) == length
    with patch.object(read, "MAX_REQUEST_BYTES", length - 1):
        with pytest.raises(read.ResourceConfigCandidateError):
            request()


def test_item_and_aggregate_account_limits():
    pairs = tuple((Kind.APP, UUID(int=i)) for i in range(500))
    candidate = request(pairs, pairs)
    assert len(read.parse_resource_config(raw([row(pair) for pair in pairs]), request=candidate).observations) == 500
    with pytest.raises(read.ResourceConfigCandidateError):
        read.parse_resource_config(raw([row(pair) for pair in pairs] + [row()]), request=candidate)
    accounts = [str(UUID(int=i)) for i in range(128)]
    rows = [row(pair, account_ids=accounts) for pair in pairs[:32]]
    assert len(read.parse_resource_config(raw(rows), request=candidate).observations) == 500
    rows.append(row(pairs[32], account_ids=[str(UUID(int=900))]))
    assert len(raw(rows)) < read.MAX_RESPONSE_BYTES
    with pytest.raises(read.ResourceConfigCandidateError):
        read.parse_resource_config(raw(rows), request=candidate)
