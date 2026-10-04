"""Offline transport-only fixtures; never activate a production capability."""

import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from core.casdoor import rbac_read as raw
from core.casdoor import rbac_reader as reader
from core.casdoor.ownership import ExternalMemberRolesProjection, has_remote_owner
from core.helper import ssrf_proxy

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
MEMBER = UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL = UUID("33333333-3333-4333-8333-333333333333")


def role(identifier="normal", tag="normal", **changes):
    return {
        "id": identifier,
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Synthetic display",
        "category": "global_system_default",
        "role_tag": tag,
        "is_builtin": True,
        "permission_keys": [],
        **changes,
    }


def member(roles=()):
    return {"account_id": str(MEMBER), "roles": list(roles)}


def page(roles=(), *, total=None, current=1, count=1, per_page=128):
    return {
        "data": list(roles),
        "pagination": {
            "total_count": len(roles) if total is None else total,
            "per_page": per_page,
            "current_page": current,
            "total_pages": count,
        },
    }


def response(value, *, status=200, headers=None):
    body = value if type(value) is bytes else json.dumps(value, separators=(",", ":")).encode()
    result = httpx.Response(status, content=body)
    result.headers.update(headers or {"content-type": "application/json"})
    return result


class Leases:
    def __init__(self, deadline=145):
        self._deadline = deadline
        self.canonical_keys = (
            f"casdoor:lease:v1:account:{MEMBER}",
            f"casdoor:lease:v1:member:{WORKSPACE}:{MEMBER}",
        )
        self.calls = 0
        self.lost = False
        self.hook = lambda: None

    def ensure_owned(self):
        self.calls += 1
        self.hook()
        if self.lost:
            raise RuntimeError("must never escape")


class Harness:
    def __init__(self, values, **context_changes):
        self.now = 100
        self.calls = []
        self.values = list(values)
        self.after_request = lambda: None
        self.leases = Leases()
        self.context = reader._OfflineReadContext(
            **{
                "workspace_id": WORKSPACE,
                "account_id": MEMBER,
                "principal_id": PRINCIPAL,
                "base_url": "https://enterprise.invalid/inner/api",
                "secret": "OFFLINE-SYNTHETIC-ONLY",
                "deadline": 145,
                **context_changes,
            }
        )
        self.operation = reader._OfflineReadOperation(
            self.context, self.leases, self.transport, monotonic=lambda: self.now
        )

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.values.pop(0)
        self.after_request()
        if isinstance(value, Exception):
            raise value
        return value

    def run(self, targets=()):
        return self.operation.extract(self.operation.read(targets))


@pytest.mark.parametrize("capability", [None, False, True, "offline_fixture_v1", object()])
def test_production_rejects_before_config_or_network(capability):
    with patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=AssertionError("must not dispatch")):
        with pytest.raises(reader.RbacReadError, match="^authorization_pending$"):
            reader.read_rbac(capability=capability, arbitrary_operation=object())


def test_actual_ssrf_owner_seam_exact_paths_headers_options_and_offline_result():
    h = Harness([response(member()), response(page([role()]))])
    # No live I/O: actual owner symbol is patched before private seam selection.
    with patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=h.transport):
        h.operation.transport = ssrf_proxy.make_request_with_deadline
        result = h.run(("normal",))
    assert result.member.roles == ()
    assert result.desired[0].roles == result.catalog.roles
    assert not isinstance(result.member, ExternalMemberRolesProjection)
    assert not hasattr(result, "knowledge")
    assert h.calls[0][0] == ("GET", "https://enterprise.invalid/inner/api/rbac/members/rbac-roles")
    assert h.calls[0][1]["params"] == {"account_id": str(MEMBER)}
    assert h.calls[1][0] == ("GET", "https://enterprise.invalid/inner/api/rbac/roles")
    assert h.calls[1][1]["params"] == {
        "page_number": 1,
        "results_per_page": 128,
        "include_owner": 1,
        "biiling_enabled": False,
        "dataset_operator_enabled": False,
    }
    for _, kwargs in h.calls:
        assert kwargs["deadline"] == 145 and kwargs["request_timeout"] == 15
        assert kwargs["max_retries"] == 0 and kwargs["follow_redirects"] is False
        assert kwargs["ssl_verify"] is True
        assert kwargs["headers"]["X-Inner-Tenant-Id"] == str(WORKSPACE)
        assert kwargs["headers"]["X-Inner-Account-Id"] == str(PRINCIPAL)
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert "OFFLINE-SYNTHETIC-ONLY" not in repr(h.operation) + repr(h.context) + repr(result)
    with pytest.raises(reader.RbacReadError):
        reader.read_rbac(capability=result)


def test_all_pages_after_100_and_exact_builtin_singleton():
    first = [role(f"custom-{index}", "", is_builtin=False) for index in range(128)]
    h = Harness(
        [
            response(member([role("owner", "owner")])),
            response(page(first, total=129, count=2)),
            response(page([role()], total=129, current=2, count=2)),
        ]
    )
    out = h.run(("normal",))
    assert has_remote_owner(out.member.roles)
    assert out.catalog.page_count == 2 and out.catalog.total_count == 129
    assert out.desired[0].roles[0].role_id == "normal"
    assert [call[1]["params"]["page_number"] for call in h.calls[1:]] == [1, 2]
    assert len({call[1]["deadline"] for call in h.calls}) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"account_id":"x","roles":[]}',
        b"\xff",
        b"[]",
        b'{"account_id":"x","roles":null}',
        b'{"roles":[],"roles":[]}',
    ],
)
def test_bad_member_stops_before_catalog_and_is_sticky(body):
    h = Harness([response(body), response(page())])
    with pytest.raises(reader.RbacReadError, match="^authorization_pending$"):
        h.run()
    with pytest.raises(reader.RbacReadError) as stopped:
        h.operation.read()
    assert stopped.value.termination_confirmed is True
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        response(member(), status=403),
        response(member(), status=302),
        response(member(), headers={"content-type": "text/html"}),
        response(member(), headers={"content-type": "application/json", "content-encoding": "gzip"}),
        response(b"x" * (raw.MAX_MEMBER_BYTES + 1)),
        RuntimeError("secret response role identifiers"),
        ssrf_proxy.RequestDeadlineExceededError("secret endpoint"),
        ssrf_proxy.ResponseTooLargeError("secret endpoint"),
    ],
)
def test_http_encoding_bounds_and_errors_safe(bad):
    h = Harness([bad])
    with pytest.raises(reader.RbacReadError, match="^authorization_pending$") as caught:
        h.run()
    assert caught.value.termination_confirmed is True
    assert caught.value.__suppress_context__


def test_unconfirmed_cleanup_flag_stops_operation():
    h = Harness([ssrf_proxy.RequestTerminationUnconfirmedError("secret")])
    with pytest.raises(reader.RbacReadError) as caught:
        h.run()
    assert caught.value.termination_confirmed is False
    with pytest.raises(reader.RbacReadError) as stopped:
        h.operation.read()
    assert stopped.value.termination_confirmed is False
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"current_page": 2},
        {"total_pages": 33},
        {"total_count": 2049},
        {"total_count": True},
        {"per_page": 100},
        {"total_pages": 2},
        {"current_page": None},
    ],
)
def test_first_page_pagination_failure_does_not_dispatch_second(change):
    body = page([role()])
    body["pagination"].update(change)
    h = Harness([response(member()), response(body)])
    with pytest.raises(reader.RbacReadError):
        h.run()
    assert len(h.calls) == 2


@pytest.mark.parametrize("stage", ["before", "after", "extract"])
@pytest.mark.parametrize("failure", ["deadline", "lease"])
def test_deadline_lease_failures_before_after_io_and_extraction(stage, failure):
    h = Harness([response(member()), response(page())])

    def invalidate():
        if failure == "deadline":
            h.now = 145
        else:
            h.leases.lost = True

    if stage == "before":
        invalidate()
    elif stage == "after":
        h.after_request = invalidate
    if stage == "extract":
        receipt = h.operation.read()
        invalidate()
        with pytest.raises(reader.RbacReadError):
            h.operation.extract(receipt)
    else:
        with pytest.raises(reader.RbacReadError):
            h.run()
    assert len(h.calls) == {"before": 0, "after": 1, "extract": 2}[stage]


def test_late_synchronous_lease_result_rejected_before_network():
    h = Harness([])
    h.leases.hook = lambda: setattr(h, "now", 145)
    with pytest.raises(reader.RbacReadError):
        h.run()
    assert not h.calls


@pytest.mark.parametrize("forgery", ["copy", "other_operation", "public_candidate", "bool"])
def test_receipt_private_identity_and_exact_operation_scope(forgery):
    h = Harness([response(member()), response(page())])
    receipt = h.operation.read()
    forged = {
        "copy": replace(receipt),
        "other_operation": reader._OfflineReceipt(object(), receipt._result),
        "public_candidate": receipt._result.member,
        "bool": True,
    }[forgery]
    with pytest.raises(reader.RbacReadError):
        h.operation.extract(forged)
    with pytest.raises(reader.RbacReadError):
        reader.read_rbac(capability=receipt)


def test_receipt_consumed_and_public_projection_never_trusted():
    h = Harness([response(member()), response(page())])
    receipt = h.operation.read()
    result = h.operation.extract(receipt)
    with pytest.raises(reader.RbacReadError):
        h.operation.extract(receipt)
    with pytest.raises(reader.RbacReadError):
        reader.read_rbac(capability=ExternalMemberRolesProjection(WORKSPACE, MEMBER), candidate=result)


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://user:password@enterprise.invalid"},
        {"base_url": "https://enterprise.invalid?key=secret"},
        {"base_url": "https://enterprise.invalid/#secret"},
        {"deadline": True},
        {"deadline": 146},
        {"deadline": 100},
        {"deadline": float("nan")},
        {"principal_id": str(PRINCIPAL)},
        {"secret": "bad\nsecret"},
        {"billing_enabled": 1},
        {"dataset_operator_enabled": "true"},
    ],
)
def test_context_invalid_never_dispatches(changes):
    with pytest.raises(reader.RbacReadError, match="^authorization_pending$"):
        Harness([], **changes)


@pytest.mark.parametrize("mutation", ["context", "deadline", "scope"])
def test_scope_or_deadline_cannot_change_after_operation_binding(mutation):
    h = Harness([])
    if mutation == "context":
        h.operation.context = replace(h.context, workspace_id=uuid4())
    elif mutation == "deadline":
        h.leases._deadline = 146
    else:
        h.leases.canonical_keys = ()
    with pytest.raises(reader.RbacReadError):
        h.run()
    assert not h.calls


@pytest.mark.parametrize("targets", [("owner",), ("normal", "normal"), (["normal"],), ["normal"]])
def test_bad_desired_targets_fail_before_http(targets):
    h = Harness([])
    with pytest.raises(reader.RbacReadError):
        h.run(targets)
    assert not h.calls


def test_catalog_aggregate_bound_before_json_parse_and_next_dispatch():
    h = Harness([response(member()), response(page())])
    with patch.object(raw, "MAX_CATALOG_BYTES", 1), patch.object(raw, "_decode", wraps=raw._decode) as decode:
        with pytest.raises(reader.RbacReadError):
            h.run()
    assert decode.call_count == 1  # Member only; oversized catalog never parsed.
    assert h.calls[-1][1]["max_response_bytes"] == 1


def test_second_page_duplicate_role_id_and_total_change_reject():
    first = [role(f"r{index}") for index in range(128)]
    for final in [page([role("r0")], total=129, current=2, count=2), page([role()], total=130, current=2, count=2)]:
        h = Harness([response(member()), response(page(first, total=129, count=2)), response(final)])
        with pytest.raises(reader.RbacReadError):
            h.run()


def test_unknown_custom_owner_label_and_missing_builtin_fail_closed():
    custom = role("custom", "owner", is_builtin=False, name="Owner")
    h = Harness([response(member([custom])), response(page([custom]))])
    with pytest.raises(reader.RbacReadError):
        h.run(("normal",))


@pytest.mark.parametrize(
    "path,params",
    [
        ("/rbac/roles/export", {}),
        ("/rbac/members/rbac-roles", {"account_id": str(PRINCIPAL)}),
        ("/rbac/roles", {"page_number": 1}),
        ("https://other.invalid", {}),
    ],
)
def test_private_request_policy_never_allows_arbitrary_endpoint_or_scope(path, params):
    h = Harness([])
    h.operation._started = True
    with pytest.raises(reader.RbacReadError):
        h.operation._request(path, params, raw.MAX_PAGE_BYTES)
    assert not h.calls


def test_missing_raw_owner_metadata_stops_before_next_page():
    roles = [role(f"r{index}") for index in range(128)]
    del roles[0]["is_builtin"]
    h = Harness([response(member()), response(page(roles, total=129, count=2)), response(page())])
    with pytest.raises(reader.RbacReadError):
        h.run()
    assert len(h.calls) == 2


def test_lease_checked_between_pages_and_no_deadline_extension():
    roles = [role(f"r{index}") for index in range(128)]
    h = Harness([response(member()), response(page(roles, total=129, count=2)), response(page())])

    def advance():
        h.now += 23

    h.after_request = advance
    with pytest.raises(reader.RbacReadError):
        h.run()
    assert len(h.calls) == 2
    assert all(kwargs["deadline"] == 145 for _, kwargs in h.calls)


def test_exact_empty_catalog_scope_without_raw_workspace_marker():
    h = Harness([response(member()), response(page())])
    out = h.run()
    assert out.member.workspace_id == out.catalog.workspace_id == WORKSPACE
    assert out.member.account_id == MEMBER and out.catalog.roles == ()
    assert not hasattr(out.member, "knowledge")
