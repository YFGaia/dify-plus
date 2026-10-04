"""Fresh independent counterexamples for the offline RBAC reader boundary."""

import json
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from core.casdoor import rbac_read as raw
from core.casdoor import rbac_reader as reader
from core.helper import ssrf_proxy

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
MEMBER = UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL = UUID("33333333-3333-4333-8333-333333333333")


def role(identifier="normal"):
    return {
        "id": identifier,
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Independent synthetic role",
        "category": "global_system_default",
        "role_tag": "normal",
        "is_builtin": True,
        "permission_keys": [],
    }


def member_body():
    return {"account_id": str(MEMBER), "roles": []}


def page(roles, *, total, current, count, per_page):
    return {
        "data": roles,
        "pagination": {
            "total_count": total,
            "per_page": per_page,
            "current_page": current,
            "total_pages": count,
        },
    }


def response(value):
    body = value if type(value) is bytes else json.dumps(value, separators=(",", ":")).encode()
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


class _Leases:
    _deadline = 145.0
    canonical_keys = (
        f"casdoor:lease:v1:account:{MEMBER}",
        f"casdoor:lease:v1:member:{WORKSPACE}:{MEMBER}",
    )

    def ensure_owned(self):
        return None


class _Harness:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []
        self.context = reader._OfflineReadContext(
            workspace_id=WORKSPACE,
            account_id=MEMBER,
            principal_id=PRINCIPAL,
            base_url="https://enterprise.invalid/inner/api",
            secret="OFFLINE-SYNTHETIC-ONLY",
            deadline=145.0,
        )
        self.leases = _Leases()
        self.operation = reader._OfflineReadOperation(
            self.context,
            self.leases,
            self.transport,
            monotonic=lambda: 100.0,
        )

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def run(self, targets=()):
        return self.operation.extract(self.operation.read(targets))


class _HostileLazyInput:
    """Fail if a closed production gate inspects caller-controlled data."""

    def __getattribute__(self, name):
        raise AssertionError("production gate inspected caller-controlled input")

    def __repr__(self):
        raise AssertionError("production gate rendered caller-controlled input")


def test_production_gate_does_not_inspect_lazy_inputs_or_dispatch():
    hostile = _HostileLazyInput()
    with (
        patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=AssertionError("must not dispatch")),
        pytest.raises(reader.RbacReadError, match="^authorization_pending$"),
    ):
        reader.read_rbac(capability=hostile, context=hostile, transport=hostile)


def test_remaining_aggregate_budget_rejects_page_before_json_parse_or_following_dispatch():
    first_page = page([role("first")], total=2, current=1, count=2, per_page=1)
    first_bytes = json.dumps(first_page, separators=(",", ":")).encode()
    second_page = page([role("second")], total=2, current=2, count=2, per_page=1)
    h = _Harness([response(member_body()), response(first_bytes), response(second_page)])

    # Page one is valid and leaves only one aggregate byte. The synthetic seam
    # deliberately ignores the requested response cap so the reader's own
    # post-transport check must reject page two before decoding it.
    with (
        patch.object(raw, "MAX_PER_PAGE", 1),
        patch.object(raw, "MAX_CATALOG_BYTES", len(first_bytes) + 1),
        patch.object(raw, "_decode", wraps=raw._decode) as decode,
        pytest.raises(reader.RbacReadError, match="^authorization_pending$"),
    ):
        h.run()

    assert decode.call_count == 2  # Member and first catalog page only.
    assert len(h.calls) == 3  # No page after the over-budget second response.
    assert h.calls[2][1]["max_response_bytes"] == 1


def test_unconfirmed_cleanup_after_member_read_stays_sticky_without_receipt_or_retry():
    h = _Harness([response(member_body()), ssrf_proxy.RequestTerminationUnconfirmedError("synthetic cleanup failure")])

    with pytest.raises(reader.RbacReadError) as caught:
        h.operation.read()
    assert caught.value.termination_confirmed is False
    assert h.operation._issued is None

    with pytest.raises(reader.RbacReadError) as retry:
        h.operation.read()
    assert retry.value.termination_confirmed is False

    with pytest.raises(reader.RbacReadError) as extraction:
        h.operation.extract(object())
    assert extraction.value.termination_confirmed is False
    assert len(h.calls) == 2


def test_offline_reader_output_contains_only_candidate_contracts():
    h = _Harness([response(member_body()), response(page([role()], total=1, current=1, count=1, per_page=128))])
    result = h.run(("normal",))

    assert result.member.contract is raw.RawRbacContract.OFFLINE_FIXTURE_V1
    assert result.catalog.contract is raw.RawRbacContract.OFFLINE_FIXTURE_V1
    assert result.desired[0].contract is raw.RawRbacContract.OFFLINE_FIXTURE_V1
    assert (result.member.workspace_id, result.member.account_id) == (WORKSPACE, MEMBER)
    assert (h.context.principal_id, h.leases._deadline) == (PRINCIPAL, h.context.deadline)
    assert not hasattr(result.member, "knowledge")
    assert not hasattr(result.catalog, "visibility")
    assert not hasattr(result.desired[0], "authenticated")
    with pytest.raises(reader.RbacReadError, match="^authorization_pending$"):
        reader.read_rbac(capability=result)
