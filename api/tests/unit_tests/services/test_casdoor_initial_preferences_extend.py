"""Preference provenance through original signed/consumed HTTP owners, offline only."""

from datetime import datetime
from urllib.parse import urlsplit

import jwt
import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor import auth_transactions as auth
from core.helper import ssrf_proxy
from models.account import Account, AccountStatus, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
)
from services.entities.account_login_entities import AuthTokenPair
from sqlalchemy.orm import Session
from test_casdoor_local_http_service_extend import begin, complete, counts
from test_casdoor_local_http_service_extend import http_flow as original_http_flow
from test_casdoor_local_http_service_extend import local_fixture as local_fixture
from test_casdoor_local_http_service_extend import login_env as login_env
from test_casdoor_local_http_service_extend import signing as signing
from test_casdoor_login_account_service_extend import seed
from test_gateway import response

http_flow = original_http_flow


def enrich_userinfo(monkeypatch, preferences):
    """Delegate the original signed provider wire and enrich only UserInfo JSON."""
    original = ssrf_proxy.make_request_with_deadline

    def transport(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if urlsplit(url).path.endswith("userinfo"):
            return response(result.json() | preferences)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)


def assert_completed_account(flow, result):
    assert result.status == 302 and result.error is None and type(result.tokens) is AuthTokenPair
    assert result.redirect == flow.settings.CONSOLE_WEB_URL + "/apps/preferences"
    assert (
        result.phases.local_outcome,
        result.phases.finalization_outcome,
        result.phases.token_outcome,
        result.phases.cleanup_released,
    ) == ("committed", "committed", "issued", True)
    assert len(flow.control.created) == len(flow.control.consumed) == 1
    assert flow.control.consumed[0].nonce == flow.control.created[0].nonce
    assert len([path for path, _ in flow.control.requests if path.endswith("access_token")]) == 1
    assert len([call for call in flow.init.calls if call[0] == auth.CONSUME_SCRIPT]) == 1
    assert len(flow.control.tokens) == 2 and not flow.lease.data
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert counts(flow) == {Account: 1, AccountMoneyExtend: 1, CasdoorIdentityExtend: 1, TenantAccountJoin: 2}
    claims = jwt.decode(result.tokens.access_token, dify_config.SECRET_KEY, algorithms=["HS256"])
    assert claims["sub"] == "Console API Passport" and result.tokens.csrf_token
    assert len(result.tokens.refresh_token) == 128
    with Session(flow.local.engine) as reader:
        account = reader.get(Account, claims["user_id"])
        assert account.status is AccountStatus.ACTIVE and account.initialized_at
        assert account.last_login_ip == "192.0.2.7" and account.last_login_at
        identity = reader.scalar(sa.select(CasdoorIdentityExtend))
        assert identity.account_id == account.id and identity.sync_generation == 1
        assert set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
            CasdoorFinalizationState.FINALIZED,
        }
        return account.interface_language, account.timezone, account.interface_theme, account.initialized_at


@pytest.mark.parametrize(
    ("navigation", "remote", "stored", "expected"),
    [
        pytest.param(
            {}, {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"}, (None, None), ("ja-JP", "Asia/Tokyo"), id="omitted-idp"
        ),
        pytest.param(
            {"locale": "invalid", "timezone": "invalid"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            (None, None),
            ("ja-JP", "Asia/Tokyo"),
            id="invalid-idp",
        ),
        pytest.param(
            {"locale": "zh-Hans", "timezone": "Europe/Paris"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            ("zh-Hans", "Europe/Paris"),
            ("zh-Hans", "Europe/Paris"),
            id="both-request-win",
        ),
        pytest.param(
            {"locale": "zh-Hans"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            ("zh-Hans", None),
            ("zh-Hans", "Asia/Tokyo"),
            id="request-language-idp-zone",
        ),
        pytest.param(
            {"timezone": "Europe/Paris"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            (None, "Europe/Paris"),
            ("ja-JP", "Europe/Paris"),
            id="idp-language-request-zone",
        ),
        pytest.param(
            {"locale": "invalid", "timezone": "Europe/Paris"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            (None, "Europe/Paris"),
            ("ja-JP", "Europe/Paris"),
            id="invalid-language-request-zone",
        ),
        pytest.param(
            {"locale": "zh-Hans", "timezone": "invalid"},
            {"locale": "ja-JP", "zoneinfo": "Asia/Tokyo"},
            ("zh-Hans", None),
            ("zh-Hans", "Asia/Tokyo"),
            id="request-language-invalid-zone",
        ),
        pytest.param({}, {}, (None, None), ("en-US", "America/New_York"), id="omitted-defaults"),
        pytest.param(
            {"locale": "invalid", "timezone": "invalid"},
            {"locale": "invalid", "zoneinfo": "invalid"},
            (None, None),
            ("en-US", "America/New_York"),
            id="invalid-defaults",
        ),
    ],
)
def test_signed_callback_initial_preference_provenance(http_flow, monkeypatch, navigation, remote, stored, expected):
    f = http_flow
    enrich_userinfo(monkeypatch, remote)
    scope, _ = begin(f, return_path="/apps/preferences", **navigation)
    persisted = assert_completed_account(f, complete(f, scope))
    assert persisted[:2] == expected
    assert persisted[2] == "light"
    context = f.control.consumed[0].context
    assert (context.locale, context.timezone) == stored


def test_signed_callback_preserves_initialized_bound_preferences(http_flow, monkeypatch):
    f = http_flow
    initialized = datetime(2025, 1, 1)
    seed(f.local.env, AccountStatus.ACTIVE, initialized)
    enrich_userinfo(monkeypatch, {"locale": "fr-FR", "zoneinfo": "Europe/Paris"})
    scope, _ = begin(f, return_path="/apps/preferences", locale="zh-Hans", timezone="Asia/Shanghai")
    assert assert_completed_account(f, complete(f, scope)) == ("ja-JP", "Asia/Tokyo", "dark", initialized)
