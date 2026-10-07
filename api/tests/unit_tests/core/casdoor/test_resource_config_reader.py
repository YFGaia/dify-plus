"""Offline fake transport contract tests; no runtime authority or external I/O."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

from core.casdoor import resource_config_read as raw
from core.casdoor import resource_config_reader as reader
from core.helper import ssrf_proxy
from repositories.casdoor_local_resource_repository_extend import LocalResourceInventory
from services.enterprise.rbac_service import RBACResourceType as Kind

W, M, P = (UUID(int=i) for i in (10000, 10001, 10002))


def inventory(n):
    return LocalResourceInventory(W, tuple((Kind.APP, UUID(int=i + 1)) for i in range(n)), 3 + (n + 499) // 500)


def response(value=b'{"data":[]}'):
    return httpx.Response(200, content=value, headers={"content-type": "application/json"})


class Leases:
    _deadline = 145.0
    canonical_keys = (f"casdoor:lease:v1:account:{M}", f"casdoor:lease:v1:member:{W}:{M}")
    lost = False

    def ensure_owned(self):
        if self.lost:
            raise RuntimeError("private lease detail")


class Harness:
    def __init__(self, transport=None):
        self.now = 100.0
        self.calls = []
        self.hook = lambda: None
        self.value = response()
        self.leases = Leases()
        self.context = reader._OfflineReadContext(
            W, M, P, "https://enterprise.invalid/inner/api", "fixture-secret", 145
        )
        self.operation = reader._OfflineReadOperation(
            self.context, self.leases, transport or self.transport, monotonic=lambda: self.now
        )

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.hook()
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def read(self, inv, selected=None):
        return self.operation.read(inv, inv.resources if selected is None else selected)


@pytest.mark.parametrize("n,requests", [(0, 0), (1, 1), (500, 1), (501, 2), (4096, 9)])
def test_batch_boundaries_and_unknown(n, requests):
    h = Harness()
    result = h.operation.extract(h.read(inventory(n)))
    assert len(h.calls) == requests == len(result.batches)
    assert sum(len(batch.observations) for batch in result.batches) == n
    assert all(
        state is raw.ResourceConfigObservation.OMITTED_UNKNOWN
        for batch in result.batches
        for _, state in batch.observations
    )
    for args, options in h.calls:
        assert args == ("POST", "https://enterprise.invalid/inner/api/rbac/whitelist/configs")
        assert 1 <= len(json.loads(options["content"])["resources"]) <= 500
        assert len(options["content"]) <= 65536
        assert options["deadline"] == 145
        assert options["request_timeout"] == 15
        assert options["max_retries"] == 0
        assert options["follow_redirects"] is False
        assert options["ssl_verify"] is True
        assert options["headers"] == {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Enterprise-Api-Secret-Key": "fixture-secret",
            "X-Inner-Tenant-Id": str(W),
            "X-Inner-Account-Id": str(P),
        }
        assert set(options) == {
            "content",
            "deadline",
            "max_response_bytes",
            "request_timeout",
            "max_retries",
            "follow_redirects",
            "ssl_verify",
            "headers",
        }


def test_actual_owner_patched_before_injection_and_kind_subset_order():
    pairs = tuple((kind, UUID(int=1)) for kind in (Kind.APP, Kind.DATASET, Kind.AGENT))
    with patch.object(ssrf_proxy, "make_request_with_deadline", return_value=response()) as fake:
        h = Harness(fake)
        result = h.operation.extract(h.read(LocalResourceInventory(W, pairs, 6), (pairs[2], pairs[0])))
    assert result.batches[0].request.pairs == (pairs[0], pairs[2])
    assert json.loads(fake.call_args.kwargs["content"])["resources"] == [
        {"resource_type": kind.value, "resource_id": str(key)} for kind, key in (pairs[0], pairs[2])
    ]


@pytest.mark.parametrize("capability", [None, False, True, object(), "offline_fixture_v1"])
def test_public_rejects_unconditionally(capability):
    with patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=AssertionError):
        with pytest.raises(reader.ResourceConfigReadError):
            reader.read_resource_config(capability=capability, configuration=lambda: pytest.fail("config access"))


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": M},
        {"workspace_id": str(W)},
        {"status": "partial"},
        {"attempted_queries": True},
        {"attempted_queries": 2},
        {"attempted_queries": 17},
        {"attempted_queries": 3},
        {"resources": (("app", UUID(int=1)),)},
        {"resources": ((Kind.APP, "bad"),)},
        {"resources": ((Kind.APP, UUID(int=1)),) * 2},
        {"resources": ((Kind.DATASET, UUID(int=1)), (Kind.APP, UUID(int=1)))},
    ],
)
def test_inventory_consistency_rejection(changes):
    h = Harness()
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(replace(inventory(1), **changes))
    assert not h.calls


def test_fabricated_consistent_inventory_is_only_synthetic_and_empty_subset_skips_http():
    h = Harness()
    result = h.operation.extract(h.read(inventory(1), ()))
    assert result.batches == () and h.calls == []
    assert not hasattr(result, "complete") and not hasattr(result, "knowledge")


@pytest.mark.parametrize("selected", [((Kind.DATASET, UUID(int=1)),), ((Kind.APP, UUID(int=1)),) * 2, []])
def test_exact_subset(selected):
    h = Harness()
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(inventory(1), selected)
    assert not h.calls


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"bad",
        b'{"data":[],"data":[]}',
        b'{"data":[],"next":"x"}',
        b'{"data":[{}]}',
        b"\xff",
        b" " * (256 * 1024 + 1),
    ],
)
def test_bad_body_stops_all_batches(body):
    h = Harness()
    h.value = response(body)
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(inventory(501))
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "field,value", [("status", 302), ("status", 500), ("content-type", "text/html"), ("content-encoding", "gzip")]
)
def test_response_contract(field, value):
    h = Harness()
    if field == "status":
        h.value.status_code = value
    else:
        h.value.headers[field] = value
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(inventory(1))


@pytest.mark.parametrize("count", [4096, 4097])
def test_cross_batch_account_budget(count):
    calls = []

    def transport(*args, **kwargs):
        requested = json.loads(kwargs["content"])["resources"]
        remaining = 4096 if not calls else count - 4096
        rows = []
        for pair in requested:
            amount = min(128, remaining)
            rows.append(
                {
                    **pair,
                    "automatic_include_workspace_members": False,
                    "account_ids": [str(UUID(int=i + 1)) for i in range(amount)],
                }
            )
            remaining -= amount
        calls.append(kwargs)
        return response(json.dumps({"data": rows}).encode())

    h = Harness(transport)
    if count == 4097:
        with pytest.raises(reader.ResourceConfigReadError):
            h.read(inventory(501))
    else:
        result = h.operation.extract(h.read(inventory(501)))
        assert sum(batch.validated_account_count for batch in result.batches) == 4096
    assert len(calls) == 2


@pytest.mark.parametrize("over", [False, True])
def test_total_raw_bytes_boundary(over):
    calls = []

    def transport(*args, **kwargs):
        size = 256 * 1024 if len(calls) < 7 else 256 * 1024 - 11
        if len(calls) == 8:
            size = 11 + int(over)
        calls.append(kwargs)
        return response(b'{"data":[]}' + b" " * (size - 11))

    h = Harness(transport)
    if over:
        with pytest.raises(reader.ResourceConfigReadError):
            h.read(inventory(4096))
    else:
        assert len(h.operation.extract(h.read(inventory(4096))).batches) == 9
    assert len(calls) == 9 and calls[-1]["max_response_bytes"] == 11


@pytest.mark.parametrize("mutation", ["context", "transport", "leases", "clock", "deadline", "keys", "lost"])
def test_identity_or_lease_swap_before_dispatch(mutation):
    h = Harness()
    if mutation == "context":
        h.operation.context = replace(h.context, principal_id=M)
    elif mutation == "transport":
        h.operation.transport = lambda *a, **k: response()
    elif mutation == "leases":
        h.operation.leases = Leases()
    elif mutation == "clock":
        h.operation.monotonic = lambda: 100
    elif mutation == "deadline":
        h.leases._deadline = 146
    elif mutation == "keys":
        h.leases.canonical_keys = ()
    else:
        h.leases.lost = True
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(inventory(1))
    assert not h.calls


@pytest.mark.parametrize("when", ["before", "after", "extract"])
def test_deadline(when):
    h = Harness()
    if when == "before":
        h.now = 145
    elif when == "after":
        h.hook = lambda: setattr(h, "now", 145)
    if when == "extract":
        receipt = h.read(inventory(1))
        h.now = 145
        with pytest.raises(reader.ResourceConfigReadError):
            h.operation.extract(receipt)
    else:
        with pytest.raises(reader.ResourceConfigReadError):
            h.read(inventory(501))
    assert len(h.calls) <= 1


def test_single_use_and_forged_receipt_invalidates_authentic():
    h = Harness()
    receipt = h.read(inventory(1))
    with pytest.raises(reader.ResourceConfigReadError):
        h.operation.extract(replace(receipt))
    with pytest.raises(reader.ResourceConfigReadError):
        h.operation.extract(receipt)
    h = Harness()
    receipt = h.read(inventory(1))
    h.operation.extract(receipt)
    with pytest.raises(reader.ResourceConfigReadError):
        h.operation.extract(receipt)


@pytest.mark.parametrize(
    "error,confirmed",
    [
        (RuntimeError("private secret response"), True),
        (ssrf_proxy.RequestTerminationUnconfirmedError("private"), False),
        (asyncio.CancelledError(), False),
    ],
)
def test_failure_and_cancellation_sticky(error, confirmed):
    h = Harness()
    h.value = error
    for _ in range(2):
        with pytest.raises(reader.ResourceConfigReadError, match="^resource_config_read_pending$") as caught:
            h.read(inventory(501))
        assert caught.value.termination_confirmed is confirmed
    assert len(h.calls) == 1


def test_later_batch_failure_discards_prefix():
    h = Harness()
    h.hook = lambda: setattr(h, "value", response(b"bad")) if len(h.calls) == 2 else None
    with pytest.raises(reader.ResourceConfigReadError):
        h.read(inventory(1001))
    assert len(h.calls) == 2 and h.operation._issued is None


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), 100, 146])
def test_invalid_initial_deadline(deadline):
    h = Harness()
    with pytest.raises(reader.ResourceConfigReadError):
        reader._OfflineReadOperation(replace(h.context, deadline=deadline), h.leases, h.transport, lambda: 100)
    assert not h.calls


@pytest.mark.parametrize("mutation", ["context", "keys", "cancel"])
def test_change_during_lease_check_cannot_dispatch(mutation):
    h = Harness()

    def hook():
        if mutation == "context":
            h.operation.context = replace(h.context, base_url="https://changed.invalid")
        elif mutation == "keys":
            h.leases.canonical_keys = ()
        else:
            raise asyncio.CancelledError()

    h.leases.ensure_owned = hook
    with pytest.raises(reader.ResourceConfigReadError) as caught:
        h.read(inventory(1))
    assert caught.value.termination_confirmed is (mutation != "cancel")
    assert not h.calls


def test_parser_deadline_and_receipt_public_rejection():
    h = Harness()
    original = raw.parse_resource_config

    def parse(*args, **kwargs):
        result = original(*args, **kwargs)
        h.now = 145
        return result

    with patch.object(raw, "parse_resource_config", side_effect=parse):
        with pytest.raises(reader.ResourceConfigReadError):
            h.read(inventory(501))
    assert len(h.calls) == 1
    h = Harness()
    receipt = h.read(inventory(1))
    with pytest.raises(reader.ResourceConfigReadError):
        reader.read_resource_config(capability=receipt)
    assert "fixture-secret" not in repr(h.context) + repr(h.operation) + repr(receipt)


def test_explicit_true_false_and_omitted_preserved():
    h = Harness()
    rows = [
        {"resource_type": "app", "resource_id": str(UUID(int=i)), "automatic_include_workspace_members": value}
        for i, value in ((1, True), (2, False))
    ]
    h.value = response(json.dumps({"data": rows}).encode())
    result = h.operation.extract(h.read(inventory(3)))
    assert tuple(state for _, state in result.batches[0].observations) == (
        raw.ResourceConfigObservation.RETURNED_TRUE,
        raw.ResourceConfigObservation.RETURNED_FALSE,
        raw.ResourceConfigObservation.OMITTED_UNKNOWN,
    )


def test_all_4096_returned_items_use_nine_requests():
    calls = []

    def transport(*args, **kwargs):
        rows = [
            {**pair, "automatic_include_workspace_members": True} for pair in json.loads(kwargs["content"])["resources"]
        ]
        calls.append(rows)
        return response(json.dumps({"data": rows}).encode())

    h = Harness(transport)
    result = h.operation.extract(h.read(inventory(4096)))
    assert len(calls) == 9
    assert sum(len(batch.observations) for batch in result.batches) == 4096


@pytest.mark.parametrize("inv", [None, object(), {"status": "LOCAL_SCAN_EXHAUSTED"}, inventory(4097)])
def test_non_inventory_or_oversized_inventory_rejects(inv):
    h = Harness()
    with pytest.raises(reader.ResourceConfigReadError):
        h.operation.read(inv, ())
    assert not h.calls
