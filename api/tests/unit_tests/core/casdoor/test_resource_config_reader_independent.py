"""Independent adversarial checks for the synthetic resource-config reader seam."""

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

WORKSPACE, ACCOUNT, PRINCIPAL = (UUID(int=value) for value in (81, 82, 83))


def inventory(size: int) -> LocalResourceInventory:
    pairs = tuple((Kind.APP, UUID(int=value + 1)) for value in range(size))
    return LocalResourceInventory(WORKSPACE, pairs, 3 + (size + 499) // 500)


def response() -> httpx.Response:
    return httpx.Response(200, content=b'{"data":[]}', headers={"content-type": "application/json"})


class LeaseProbe:
    _deadline = 145.0
    canonical_keys = (
        f"casdoor:lease:v1:account:{ACCOUNT}",
        f"casdoor:lease:v1:member:{WORKSPACE}:{ACCOUNT}",
    )

    def ensure_owned(self) -> None:
        return None


class Fixture:
    def __init__(self, transport=None):
        self.now = 100.0
        self.calls = []
        self.context = reader._OfflineReadContext(
            WORKSPACE, ACCOUNT, PRINCIPAL, "https://enterprise.invalid/inner/api", "fixture-secret", 145.0
        )
        self.leases = LeaseProbe()
        self.transport = transport or self.dispatch
        self.operation = reader._OfflineReadOperation(
            self.context, self.leases, self.transport, monotonic=lambda: self.now
        )

    def dispatch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return response()


@pytest.mark.parametrize("mutation", ["context", "keys"])
def test_post_dispatch_binding_change_discards_response_before_parse_or_next_batch(mutation):
    holder = {}

    def transport(*args, **kwargs):
        fixture = holder["fixture"]
        fixture.calls.append((args, kwargs))
        if mutation == "context":
            fixture.operation.context = replace(fixture.context, principal_id=UUID(int=84))
        else:
            fixture.leases.canonical_keys = ()
        return response()

    fixture = Fixture(transport)
    holder["fixture"] = fixture
    with patch.object(raw, "parse_resource_config", wraps=raw.parse_resource_config) as parse:
        with pytest.raises(reader.ResourceConfigReadError, match="^resource_config_read_pending$"):
            fixture.operation.read(inventory(1001), inventory(1001).resources)

    assert len(fixture.calls) == 1
    parse.assert_not_called()
    assert fixture.operation._issued is None


def test_cancellation_on_second_batch_stops_dispatch_and_keeps_termination_unconfirmed():
    fixture = Fixture()

    def cancel_second_batch(*args, **kwargs):
        fixture.calls.append((args, kwargs))
        if len(fixture.calls) == 2:
            raise asyncio.CancelledError()
        return response()

    fixture = Fixture(cancel_second_batch)
    with pytest.raises(reader.ResourceConfigReadError, match="^resource_config_read_pending$") as error:
        fixture.operation.read(inventory(2001), inventory(2001).resources)

    assert error.value.termination_confirmed is False
    assert len(fixture.calls) == 2
    assert fixture.operation._issued is None
    with pytest.raises(reader.ResourceConfigReadError) as retry:
        fixture.operation.read(inventory(2001), inventory(2001).resources)
    assert retry.value.termination_confirmed is False
    assert len(fixture.calls) == 2


def test_public_entry_rejects_before_any_configuration_lease_or_transport_access():
    def forbidden(*args, **kwargs):
        pytest.fail("production entry touched a disabled dependency")

    with (
        patch.object(reader, "_OfflineReadOperation", side_effect=forbidden) as operation,
        patch.object(reader, "urlsplit", side_effect=forbidden) as parse_url,
        patch.object(ssrf_proxy, "make_request_with_deadline", side_effect=forbidden) as ssrf,
    ):
        with pytest.raises(reader.ResourceConfigReadError, match="^resource_config_read_pending$"):
            reader.read_resource_config(
                capability=object(),
                configuration=forbidden,
                credential=object(),
                leases=forbidden,
                transport=forbidden,
                inventory=object(),
                selected=object(),
            )

    operation.assert_not_called()
    parse_url.assert_not_called()
    ssrf.assert_not_called()


def test_cross_batch_account_metadata_limit_rejects_entire_result_before_third_request():
    calls = []

    def account_rows(*args, **kwargs):
        pairs = json.loads(kwargs["content"])["resources"]
        remaining = 4096 if not calls else 1
        rows = []
        for pair in pairs:
            count = min(128, remaining)
            rows.append(
                {
                    **pair,
                    "automatic_include_workspace_members": False,
                    "account_ids": [str(UUID(int=index + 1)) for index in range(count)],
                }
            )
            remaining -= count
        calls.append(rows)
        return httpx.Response(
            200,
            content=json.dumps({"data": rows}).encode(),
            headers={"content-type": "application/json"},
        )

    fixture = Fixture(account_rows)
    with pytest.raises(reader.ResourceConfigReadError, match="^resource_config_read_pending$"):
        fixture.operation.read(inventory(501), inventory(501).resources)

    assert len(calls) == 2
    assert fixture.operation._issued is None
