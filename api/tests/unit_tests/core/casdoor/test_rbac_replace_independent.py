"""Independent C1 counterexamples for post-dispatch uncertainty boundaries."""

import asyncio
import json
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from core.casdoor import rbac_read as raw
from core.casdoor import rbac_reader as reader
from core.casdoor import rbac_replace as writer
from core.helper import ssrf_proxy
from models.account import TenantAccountRole

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
MEMBER = UUID("22222222-2222-4222-8222-222222222222")
PRINCIPAL = UUID("33333333-3333-4333-8333-333333333333")


def _role():
    return {
        "id": "normal-id",
        "tenant_id": str(WORKSPACE),
        "type": "workspace",
        "name": "Offline display",
        "category": "global_system_default",
        "role_tag": "normal",
        "is_builtin": True,
        "permission_keys": [],
    }


def _member(roles=()):
    return json.dumps({"account_id": str(MEMBER), "roles": list(roles)}, separators=(",", ":")).encode()


class _Leases:
    _deadline = 145
    canonical_keys = (
        f"casdoor:lease:v1:account:{MEMBER}",
        f"casdoor:lease:v1:member:{WORKSPACE}:{MEMBER}",
    )

    def ensure_owned(self):
        return None


class _Harness:
    def __init__(self, outcome=None):
        self.now = 100
        self.calls = []
        body = _member([_role()]) if outcome is None else outcome
        self.outcome = httpx.Response(200, content=body) if type(body) is bytes else body
        if isinstance(self.outcome, httpx.Response):
            self.outcome.headers["content-type"] = "application/json"
        self.after_request = lambda: None
        self.context = reader._OfflineReadContext(
            workspace_id=WORKSPACE,
            account_id=MEMBER,
            principal_id=PRINCIPAL,
            base_url="https://enterprise.invalid/inner/api",
            secret="OFFLINE-SYNTHETIC-SECRET",
            deadline=145,
        )
        self.leases = _Leases()
        self.operation = writer._OfflineReplaceOperation(
            self.context, self.leases, self.transport, monotonic=lambda: self.now
        )

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.after_request()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def attempt(self):
        role = _role()
        catalog = json.dumps(
            {
                "data": [role],
                "pagination": {"total_count": 1, "per_page": 128, "current_page": 1, "total_pages": 1},
            },
            separators=(",", ":"),
        ).encode()
        return self.operation.replace(
            prior_member=_member(),
            catalog_pages=(catalog,),
            target_role="normal",
            local_join_role=TenantAccountRole.NORMAL,
        )


@pytest.mark.parametrize("mutation", ["context", "leases", "transport"])
def test_post_dispatch_owner_mutation_discards_matching_ack(mutation):
    harness = _Harness()

    def mutate_after_dispatch():
        if mutation == "context":
            harness.operation.context = replace(harness.context, workspace_id=uuid4())
        elif mutation == "leases":
            harness.operation.leases = object()
        else:
            harness.operation.transport = lambda *args, **kwargs: harness.outcome

    harness.after_request = mutate_after_dispatch
    with pytest.raises(writer.RbacReplaceError) as failure:
        harness.attempt()

    assert failure.value.business_unknown is True
    assert failure.value.possibly_submitted is True
    assert failure.value.client_termination_confirmed is True
    assert len(harness.calls) == 1
    assert harness.operation._issued is None


def test_client_deadline_cleanup_can_be_confirmed_while_put_result_is_unknown():
    harness = _Harness(ssrf_proxy.RequestDeadlineExceededError("offline timeout"))

    with pytest.raises(writer.RbacReplaceError) as failure:
        harness.attempt()

    assert failure.value.client_termination_confirmed is True
    assert failure.value.business_unknown is True
    assert len(harness.calls) == 1
    with pytest.raises(writer.RbacReplaceError) as stopped:
        harness.operation.extract(object())
    assert stopped.value.business_unknown is True
    assert len(harness.calls) == 1


def test_ack_parser_cancellation_after_dispatch_never_issues_receipt_or_retries(monkeypatch):
    harness = _Harness()
    decode = raw.decode_member_candidate
    calls = 0

    def cancel_during_ack(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError("offline parser cancellation")
        return decode(*args, **kwargs)

    monkeypatch.setattr(raw, "decode_member_candidate", cancel_during_ack)
    with pytest.raises(writer.RbacReplaceError) as failure:
        harness.attempt()

    assert failure.value.business_unknown is True
    assert failure.value.client_termination_confirmed is True
    assert len(harness.calls) == 1
    assert harness.operation._issued is None
    with pytest.raises(writer.RbacReplaceError) as stopped:
        harness.attempt()
    assert stopped.value.business_unknown is True
    assert len(harness.calls) == 1
