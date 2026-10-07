"""Offline counterexamples for C1; no production capability or network access."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from core.casdoor import rbac_read as raw
from core.casdoor import rbac_reader as reader
from core.casdoor import rbac_replace as writer
from core.casdoor.ownership import ExternalMemberRolesProjection
from core.helper import ssrf_proxy
from models.account import TenantAccountRole

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
MEMBER = UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL = UUID("33333333-3333-4333-8333-333333333333")


def role(identifier="normal-id", tag="normal", **changes):
    return {
        "id": identifier,
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Offline display",
        "category": "global_system_default",
        "role_tag": tag,
        "is_builtin": True,
        "permission_keys": [],
        **changes,
    }


def encoded(value):
    return value if type(value) is bytes else json.dumps(value, separators=(",", ":")).encode()


def member(roles=(), **changes):
    return encoded({"account_id": str(MEMBER), "roles": list(roles), **changes})


def catalog(roles=None):
    values = [role()] if roles is None else roles
    return (
        encoded(
            {
                "data": values,
                "pagination": {"total_count": len(values), "per_page": 128, "current_page": 1, "total_pages": 1},
            }
        ),
    )


def response(body=None, *, status=200, headers=None):
    result = httpx.Response(status, content=member([role()]) if body is None else encoded(body))
    result.headers.update(headers or {"content-type": "application/json"})
    return result


class Leases:
    def __init__(self):
        self._deadline = 145
        self.canonical_keys = (
            f"casdoor:lease:v1:account:{MEMBER}",
            f"casdoor:lease:v1:member:{WORKSPACE}:{MEMBER}",
        )
        self.lost = False
        self.hook = lambda: None

    def ensure_owned(self):
        self.hook()
        if self.lost:
            raise RuntimeError("OFFLINE-SYNTHETIC-SECRET")


class Harness:
    def __init__(self, outcome=None, **context_changes):
        self.now = 100
        self.calls = []
        self.outcome = response() if outcome is None else outcome
        self.after_request = lambda: None
        self.leases = Leases()
        self.context = reader._OfflineReadContext(
            **{
                "workspace_id": WORKSPACE,
                "account_id": MEMBER,
                "principal_id": PRINCIPAL,
                "base_url": "https://enterprise.invalid/inner/api",
                "secret": "OFFLINE-SYNTHETIC-SECRET",
                "deadline": 145,
                **context_changes,
            }
        )
        self.operation = writer._OfflineReplaceOperation(
            self.context, self.leases, self.transport, monotonic=lambda: self.now
        )

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.after_request()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def attempt(self, **changes):
        return self.operation.replace(
            **{
                "prior_member": member(),
                "catalog_pages": catalog(),
                "target_role": "normal",
                "local_join_role": TenantAccountRole.NORMAL,
                **changes,
            }
        )


class Hostile:
    def __getattribute__(self, name):
        raise AssertionError("closed gate must not inspect")

    def __bool__(self):
        raise AssertionError("closed gate must not inspect")


@pytest.mark.parametrize("capability", [None, True, False, "offline_fixture_v1", "hostile", object()])
def test_production_gate_rejects_before_inspecting_any_argument(capability):
    if capability == "hostile":
        capability = Hostile()
    with patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=AssertionError("no HTTP")):
        with pytest.raises(writer.RbacReplaceError, match="^authorization_pending$") as failure:
            writer.replace_rbac(capability=capability, context=Hostile(), transport=Hostile())
    assert failure.value.possibly_submitted is False
    assert failure.value.business_unknown is False


@pytest.mark.parametrize("target", ["admin", "editor", "normal"])
def test_actual_ssrf_symbol_exact_singleton_put_and_no_completion_claim(target):
    selected = role(f"exact-{target}", target)
    h = Harness(response(member([selected])))
    # The actual shared symbol is patched before construction; no socket runs.
    with patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=h.transport):
        h.operation = writer._OfflineReplaceOperation(
            h.context, h.leases, ssrf_proxy.make_request_with_deadline, monotonic=lambda: h.now
        )
        receipt = h.attempt(catalog_pages=catalog([selected]), target_role=target)
        out = h.operation.extract(receipt)
    assert len(h.calls) == 1
    args, kwargs = h.calls[0]
    assert args == ("PUT", "https://enterprise.invalid/inner/api/rbac/members/rbac-roles")
    assert kwargs == {
        "deadline": 145,
        "max_response_bytes": 256 * 1024,
        "request_timeout": 15.0,
        "max_retries": 0,
        "follow_redirects": False,
        "ssl_verify": True,
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Enterprise-Api-Secret-Key": "OFFLINE-SYNTHETIC-SECRET",
            "X-Inner-Tenant-Id": str(WORKSPACE),
            "X-Inner-Account-Id": str(PRINCIPAL),
        },
        "params": {"account_id": str(MEMBER)},
        "json": {"role_ids": [f"exact-{target}"]},
    }
    assert out.member.roles == out.desired.roles
    assert out.desired.join_role is TenantAccountRole.NORMAL
    assert out.member.contract is raw.RawRbacContract.OFFLINE_FIXTURE_V1
    assert not isinstance(out.member, ExternalMemberRolesProjection)
    for name in ("applied", "confirmed", "knowledge", "finalized", "termination_confirmed", "cookie"):
        assert not hasattr(out, name)
    assert "OFFLINE-SYNTHETIC-SECRET" not in repr(h.operation) + repr(h.context) + repr(receipt) + repr(out)
    for value in (out, receipt, out.member, out.desired):
        with pytest.raises(writer.RbacReplaceError):
            writer.replace_rbac(capability=value)
    with pytest.raises(writer.RbacReplaceError) as failure:
        h.attempt()
    assert failure.value.business_unknown is True and len(h.calls) == 1


@pytest.mark.parametrize(
    "join", [None, True, "normal", TenantAccountRole.ADMIN, TenantAccountRole.EDITOR, TenantAccountRole.OWNER]
)
def test_only_actual_normal_local_join_admitted(join):
    h = Harness()
    with pytest.raises(writer.RbacReplaceError) as failure:
        h.attempt(local_join_role=join)
    assert not failure.value.business_unknown and h.calls == []


def test_exact_remote_owner_blocks_even_normal_join_but_custom_owner_label_does_not():
    h = Harness()
    with pytest.raises(writer.RbacReplaceError):
        h.attempt(prior_member=member([role("owner-id", "owner")]))
    assert h.calls == []
    h = Harness()
    custom = role("custom", "owner", is_builtin=False, category="custom", name="Owner")
    assert h.operation.extract(h.attempt(prior_member=member([custom]))).desired.target_role == "normal"
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "prior",
    [
        b"{}",
        b"null",
        b"\xff",
        b" " * (raw.MAX_MEMBER_BYTES + 1),
        member(account_id=str(uuid4())),
        encoded({"account_id": str(MEMBER), "roles": None}),
        member([role(permission_keys=None)]),
        member([role(is_builtin=1)]),
        member([role(tenant_id=str(uuid4()))]),
        member([role(), role()]),
        member().replace(b'"roles":[]', b'"roles":[],"roles":[]'),
    ],
)
def test_prior_unknown_raw_scope_and_metadata_refuse_before_put(prior):
    h = Harness()
    with pytest.raises(writer.RbacReplaceError) as failure:
        h.attempt(prior_member=prior)
    assert not failure.value.possibly_submitted and h.calls == []


@pytest.mark.parametrize("target", [None, True, "owner", "custom", "normal-id", "", (), []])
def test_no_arbitrary_role_id_owner_or_empty_target(target):
    h = Harness()
    with pytest.raises(writer.RbacReplaceError):
        h.attempt(target_role=target)
    assert h.calls == []


@pytest.mark.parametrize(
    "pages",
    [
        (),
        catalog([]),
        catalog([role(is_builtin=False)]),
        catalog([role(), role("second-normal")]),
        catalog([role(tenant_id=str(uuid4()))]),
        (encoded({"data": [role()]}),),
        (catalog()[0].replace(b'"total_count":1', b'"total_count":true'),),
        (catalog()[0].replace(b'"total_pages":1', b'"total_pages":2'),),
    ],
)
def test_catalog_full_unique_builtin_required(pages):
    h = Harness()
    with pytest.raises(writer.RbacReplaceError):
        h.attempt(catalog_pages=pages)
    assert h.calls == []


def test_desired_builtin_after_first_128_catalog_roles_replaces_only_exact_singleton():
    h = Harness()
    first = [role(f"custom-{index}", "", is_builtin=False, category="custom") for index in range(128)]
    pages = tuple(
        encoded(
            {
                "data": values,
                "pagination": {"total_count": 129, "per_page": 128, "current_page": number, "total_pages": 2},
            }
        )
        for number, values in ((1, first), (2, [role()]))
    )
    h.attempt(catalog_pages=pages)
    assert len(h.calls) == 1 and h.calls[0][1]["json"] == {"role_ids": ["normal-id"]}


def test_preflight_failure_permanently_stops_operation_without_any_submission():
    h = Harness()
    with pytest.raises(writer.RbacReplaceError):
        h.attempt(prior_member=b"{}")
    for action in (h.attempt, lambda: h.operation.extract(object())):
        with pytest.raises(writer.RbacReplaceError) as failure:
            action()
        assert failure.value.possibly_submitted is False and failure.value.business_unknown is False
    assert h.calls == []


def test_public_candidates_never_accepted_as_raw_inputs():
    h = Harness()
    candidate = raw.decode_member_candidate(
        member(), workspace_id=WORKSPACE, account_id=MEMBER, contract=raw.RawRbacContract.OFFLINE_FIXTURE_V1
    )
    with pytest.raises(writer.RbacReplaceError):
        h.attempt(prior_member=candidate)
    assert h.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("OFFLINE-SYNTHETIC-SECRET"),
        RuntimeError("OFFLINE-SYNTHETIC-SECRET"),
        asyncio.CancelledError("OFFLINE-SYNTHETIC-SECRET"),
        KeyboardInterrupt("OFFLINE-SYNTHETIC-SECRET"),
        ssrf_proxy.RequestDeadlineExceededError("client cleanup confirmed"),
        response(status=302),
        response(status=400),
        response(status=500),
        response(status=204),
        response(status=201),
        response(headers={"content-type": "text/html"}),
        response(headers={"content-type": "application/json;charset=utf-16"}),
        response(headers={"content-type": "application/json", "content-encoding": "gzip"}),
        response(b"{}"),
        response(b"\xff"),
        response(b"{"),
        response(member()),
        response(member(account_id=str(uuid4()))),
        response(member([role("different-id")])),
        response(member([role(permission_keys=["unexpected"])])),
        response(member([role(permission_keys=None)])),
        response(member([role(), role()])),
        response(member().replace(b'"roles":[]', b'"roles":[],"roles":[]')),
        response(b" " * (raw.MAX_MEMBER_BYTES + 1)),
        object(),
    ],
)
def test_any_possibly_sent_failure_is_sticky_unknown_even_with_client_cleanup(outcome):
    h = Harness(outcome)
    with pytest.raises(writer.RbacReplaceError, match="^authorization_pending$") as failure:
        h.attempt()
    assert failure.value.possibly_submitted is True
    assert failure.value.business_unknown is True
    assert "OFFLINE-SYNTHETIC-SECRET" not in repr(failure.value)
    for action in (h.attempt, lambda: h.operation.extract(object())):
        with pytest.raises(writer.RbacReplaceError) as stopped:
            action()
        assert stopped.value.business_unknown is True
    assert len(h.calls) == 1 and h.operation._issued is None


def test_unconfirmed_client_cleanup_flag_stays_false_separate_from_business_unknown():
    h = Harness(ssrf_proxy.RequestTerminationUnconfirmedError("OFFLINE-SYNTHETIC-SECRET"))
    for action in (h.attempt, h.attempt, lambda: h.operation.extract(object())):
        with pytest.raises(writer.RbacReplaceError) as failure:
            action()
        assert failure.value.business_unknown is True
        assert failure.value.client_termination_confirmed is False
    assert len(h.calls) == 1


@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("failure", ["lease", "deadline"])
def test_lease_or_deadline_pre_and_post_dispatch(stage, failure):
    h = Harness()

    def stop():
        if failure == "lease":
            h.leases.lost = True
        else:
            h.now = 145

    if stage == "before":
        stop()
    else:
        h.after_request = stop
    with pytest.raises(writer.RbacReplaceError) as error:
        h.attempt()
    assert error.value.business_unknown is (stage == "after")
    assert len(h.calls) == (stage == "after")


def test_budget_not_reset_after_offline_prior_read_and_late_parser_blocks_dispatch():
    h = Harness()
    h.now = 130  # Caller has already spent 30s of the shared operation.
    h.operation = writer._OfflineReplaceOperation(h.context, h.leases, h.transport, monotonic=lambda: h.now)
    h.attempt()
    assert h.calls[0][1]["deadline"] == 145
    h = Harness()
    original = raw.decode_catalog_candidate

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        h.now = 145
        return result

    with patch.object(raw, "decode_catalog_candidate", side_effect=late):
        with pytest.raises(writer.RbacReplaceError) as failure:
            h.attempt()
    assert failure.value.business_unknown is False and h.calls == []


def test_late_ack_parser_halts_after_dispatch_and_no_receipt_is_issued():
    h = Harness()
    original = raw.decode_member_candidate
    parses = 0

    def late(*args, **kwargs):
        nonlocal parses
        parses += 1
        result = original(*args, **kwargs)
        if parses == 2:
            h.now = 145
        return result

    with patch.object(raw, "decode_member_candidate", side_effect=late):
        with pytest.raises(writer.RbacReplaceError) as failure:
            h.attempt()
    assert failure.value.business_unknown is True
    assert len(h.calls) == 1 and h.operation._issued is None


def test_oversized_ack_rejected_before_json_parsing():
    body = b" " * (raw.MAX_MEMBER_BYTES + 1)
    h = Harness(response(body))
    original = raw._decode

    def guarded(value):
        assert value is not body
        return original(value)

    with patch.object(raw, "_decode", side_effect=guarded):
        with pytest.raises(writer.RbacReplaceError) as failure:
            h.attempt()
    assert failure.value.business_unknown is True and len(h.calls) == 1


@pytest.mark.parametrize("loss", ["lease", "deadline"])
def test_receipt_extraction_rechecks_and_never_turns_late_ack_into_completion(loss):
    h = Harness()
    receipt = h.attempt()
    if loss == "lease":
        h.leases.lost = True
    else:
        h.now = 145
    with pytest.raises(writer.RbacReplaceError) as failure:
        h.operation.extract(receipt)
    assert failure.value.business_unknown is True
    assert len(h.calls) == 1


@pytest.mark.parametrize("forgery", ["copy", "cross_operation", "public_candidate", "bool", "replay"])
def test_receipt_exact_identity_domain_and_single_use(forgery):
    h = Harness()
    receipt = h.attempt()
    if forgery == "copy":
        value = replace(receipt)
    elif forgery == "cross_operation":
        value = Harness().attempt()
    elif forgery == "public_candidate":
        value = receipt._result.member
    elif forgery == "bool":
        value = True
    else:
        h.operation.extract(receipt)
        value = receipt
    with pytest.raises(writer.RbacReplaceError):
        h.operation.extract(value)
    assert len(h.calls) == 1 and h.operation._issued is None


@pytest.mark.parametrize("change", ["context", "leases", "transport", "keys", "lease_deadline", "slow_lease"])
def test_mutated_scope_or_bound_owner_and_late_sync_lease_stop_before_dispatch(change):
    h = Harness()
    if change == "context":
        h.operation.context = replace(h.context, workspace_id=uuid4())
    elif change == "leases":
        h.operation.leases = Leases()
    elif change == "transport":
        h.operation.transport = lambda *args, **kwargs: response()
    elif change == "keys":
        h.leases.canonical_keys = (f"casdoor:lease:v1:account:{MEMBER}",)
    elif change == "lease_deadline":
        h.leases._deadline = 146
    else:
        h.leases.hook = lambda: setattr(h, "now", 145)
    with pytest.raises(writer.RbacReplaceError) as failure:
        h.attempt()
    assert failure.value.business_unknown is False and h.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": str(WORKSPACE)},
        {"account_id": True},
        {"principal_id": None},
        {"base_url": "https://user:password@enterprise.invalid/inner/api"},
        {"base_url": "https://enterprise.invalid/inner/api?unsafe=1"},
        {"secret": "line\nbreak"},
        {"deadline": 146},
        {"deadline": 100},
        {"deadline": float("nan")},
    ],
)
def test_constructor_reuses_b2_context_and_absolute_deadline_rejections(changes):
    with pytest.raises(writer.RbacReplaceError) as failure:
        Harness(**changes)
    assert failure.value.business_unknown is False
