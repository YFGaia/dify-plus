"""Fresh regressions for canonical records returned after modeled atomic spend."""

import json

import pytest
import test_auth_initialization as author
from core.casdoor import auth_transactions as auth

pytest_plugins = ("test_auth_initialization",)


@pytest.mark.parametrize("malformation", ["outer_whitespace", "schema_number_alias"])
def test_lua_accepted_but_python_rejected_envelope_stays_spent(env, malformation):
    handle = author.create(env)
    key, envelope, expiry = author.record(env)
    raw = env.raw.records[key][0]
    if malformation == "outer_whitespace":
        raw = " " + raw
    else:
        envelope["schema_version"] = 1.0
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    author.replace_record(env, key, raw, expiry)

    with pytest.raises(auth.AuthTransactionError, match="record_invalid"):
        author.consume(env, handle)

    # The wire model accepted the decoded closed envelope and deleted it before
    # Python's stricter canonical/type checks rejected the returned bytes.
    assert key not in env.raw.records
    assert env.raw.calls[-1][0] == auth.CONSUME_INITIALIZATION_SCRIPT
    assert len(env.raw.calls) == 2
    assert not env.raw.slots
    env.encrypt.assert_not_called()
    env.decrypt.assert_not_called()
