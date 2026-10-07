"""Independent frozen D07 gates using mounted Flask routes and real owners.

All provider/Redis responses are deterministic synthetic data; no external I/O.
"""

import json
import secrets
import time
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorIdentityExtend

pytest_plugins = ("test_casdoor_diagnostic_flow_extend",)

IDENTITY = "/console/api/account/casdoor-identity"
CALLBACK = "/console/api/auth/casdoor/callback"


@pytest.fixture
def actions(diagnostic):
    diagnostic.services.casdoor_identity_action._redis_runtime_factory = diagnostic.f.service._redis_runtime_factory
    return diagnostic


def _send(d, path, *, method="GET", **kwargs):
    return d.client.open(
        path,
        method=method,
        headers={"X-CSRF-Token": d.csrf},
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
        **kwargs,
    )


def _begin_link(d):
    response = _send(d, IDENTITY + "/link", method="POST", json={})
    assert response.status_code == 200, response.json
    handoff = _send(d, response.json["handoff_path"])
    assert handoff.status_code == 302, handoff.json
    state = parse_qs(urlsplit(handoff.location).query)["state"][0]
    return state


def _callback(d, state):
    selected = next((created for created in d.f.control.created if created.state == state), None)
    if selected is not None:
        d.f.control.created.remove(selected)
        d.f.control.created.append(selected)
    return _send(d, CALLBACK, query_string={"state": state, "code": "synthetic-code"})


def _identity_count(d):
    with d.f.service._session_factory() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend))


def test_mounted_link_pair_survives_overlapping_browser_transactions(actions):
    from core.casdoor.auth_transactions import COOKIE_PATH, SCOPE_COOKIE_NAME

    d = actions
    first = _begin_link(d)
    scope = d.client.get_cookie(SCOPE_COOKIE_NAME, domain="console.example.test", path=COOKIE_PATH)
    second = _begin_link(d)
    current_scope = d.client.get_cookie(SCOPE_COOKIE_NAME, domain="console.example.test", path=COOKIE_PATH)
    assert scope is not None and current_scope is not None and current_scope.value == scope.value

    # Complete out of order: both independent one-use transactions must survive.
    assert _callback(d, second).location.endswith("=linked")
    assert _callback(d, first).location.endswith("=linked")
    assert _identity_count(d) == 1


def test_mounted_final_sql_barrier_rechecks_original_refresh_after_flush(actions):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    d = actions
    before = _identity_count(d)
    state = _begin_link(d)
    fired = []

    def revoke_after_identity_flush(session, flush_context):
        if any(isinstance(row, CasdoorIdentityExtend) for row in session.new):
            d.source.pop("refresh_token:" + d.token, None)
            fired.append(True)

    event.listen(Session, "after_flush", revoke_after_identity_flush)
    try:
        response = _callback(d, state)
    finally:
        event.remove(Session, "after_flush", revoke_after_identity_flush)

    assert fired
    assert response.status_code in {302, 400}
    if response.status_code == 302:
        assert "linked" not in response.location
    assert _identity_count(d) == before


class _ProofWire:
    """Minimal single-key store for the production Lua contracts."""

    def __init__(self):
        self.values = {}
        self.expiry = {}

    def _get_prefix(self):
        return "independent-proof"

    def eval(self, script, count, *args):
        from core.casdoor.auth_transactions import CREATE_INITIALIZATION_SCRIPT, CONSUME_INITIALIZATION_SCRIPT

        assert count == 1
        key, *values = args
        if script == CREATE_INITIALIZATION_SCRIPT:
            if key in self.values:
                return 0
            self.values[key] = values[0]
            self.expiry[key] = time.time() + 60
            return 1
        if script == "return redis.call('GET', KEYS[1])":
            return self.values.get(key)
        if script == CONSUME_INITIALIZATION_SCRIPT:
            raw = self.values.get(key)
            if raw is None or self.expiry.get(key, 0) <= time.time():
                return None
            record = json.loads(raw)
            if record.get("owner") != values[0] or record.get("context") != values[1]:
                return None
            del self.values[key]
            self.expiry.pop(key, None)
            return raw
        raise AssertionError("unexpected proof script")


def test_real_unlink_proof_is_browser_identity_action_bound_and_one_use():
    import secrets

    from core.casdoor.auth_transactions import (
        COOKIE_PATH,
        AuthMode,
        AuthTransactionError,
        AuthTransactionStore,
        CurrentAuthContext,
        SourceSessionContext,
        TrustedAuthContext,
    )
    from core.casdoor.crypto import CasdoorCrypto
    from extensions.ext_redis import RedisClientWrapper

    namespace, revision, account = UUID(int=11), UUID(int=22), UUID(int=33)
    callback = "https://console.example.test" + COOKIE_PATH + "/callback"
    browser = secrets.token_urlsafe(32)
    wire = _ProofWire()
    redis = RedisClientWrapper()
    redis.initialize(wire)
    redis._get_prefix = wire._get_prefix
    now = datetime.now(UTC)
    protector = CasdoorCrypto(secret_key=secrets.token_bytes(32), key_version="independent-proof")
    store = AuthTransactionStore(redis, protector, clock=lambda: now)
    source = SourceSessionContext(account, account, account, protector.refresh_source_digest("live-refresh"), False)
    identity = uuid4()
    binding = "a" * 64
    context = TrustedAuthContext(
        namespace, revision, AuthMode.REAUTH_UNLINK, callback, "/account", source=source,
        identity_id=identity, action="unlink", action_binding=binding,
    )
    current = CurrentAuthContext(
        namespace, revision, AuthMode.REAUTH_UNLINK, callback, source,
        identity_id=identity, action="unlink", action_binding=binding, allowed=True,
    )
    guard_state = [current]
    guard = lambda: guard_state[0]
    proof = store.create_unlink_proof(
        context, browser_scope=browser, auth_time=now.timestamp(), guard=guard
    )

    with pytest.raises(AuthTransactionError):
        store.unlink_proof_hint(proof, browser_scope=browser + "x")
    guard_state[0] = replace(current, identity_id=uuid4())
    with pytest.raises(AuthTransactionError):
        store.consume_unlink_proof(proof, browser_scope=browser, guard=guard)
    guard_state[0] = replace(current, action="link")
    with pytest.raises(AuthTransactionError):
        store.consume_unlink_proof(proof, browser_scope=browser, guard=guard)
    guard_state[0] = current
    assert store.consume_unlink_proof(proof, browser_scope=browser, guard=guard) == context
    with pytest.raises((AuthTransactionError, ValueError)):
        store.unlink_proof_hint(proof, browser_scope=browser)
    assert wire.values == {}
