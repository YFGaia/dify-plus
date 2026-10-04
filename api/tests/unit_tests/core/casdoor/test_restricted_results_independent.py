"""Independent cross-ticket check for the restricted-result storage owner."""

from dataclasses import replace

import pytest
from core.casdoor.errors import CasdoorErrorCode
from test_restricted_results import auth
from test_restricted_results import consume as consume_result
from test_restricted_results import create as create_result

pytest_plugins = ("test_restricted_results",)


def test_valid_nonce_from_another_result_cannot_consume_either_record(env):
    first_payload = replace(
        env.payload,
        code=CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN,
        correlation_id="41000000-0000-4000-8000-000000000001",
    )
    second_payload = replace(
        env.payload,
        code=CasdoorErrorCode.WORKSPACE_UNAVAILABLE,
        correlation_id="42000000-0000-4000-8000-000000000002",
    )

    first = create_result(env, payload=first_payload)
    second = create_result(env, payload=second_payload)
    assert first.scope_cookie.value == second.scope_cookie.value == env.scope
    assert first.handoff != second.handoff
    assert first.result_cookie.value != second.result_cookie.value
    before_wrong_nonce = env.raw.records.copy()

    with pytest.raises(auth.AuthTransactionError):
        consume_result(env, first, nonce=second.result_cookie.value)
    assert env.raw.records == before_wrong_nonce

    consumed_first = consume_result(env, first)
    assert consumed_first.payload == first_payload
    assert consumed_first.clear_cookie == replace(first.result_cookie, value="", max_age=0)
    assert len(env.raw.records) == 1

    consumed_second = consume_result(env, second)
    assert consumed_second.payload == second_payload
    assert consumed_second.clear_cookie == replace(second.result_cookie, value="", max_age=0)
    assert not env.raw.records
