from urllib.parse import urlsplit

import jwt
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
from test_gateway import response

http_flow = original_http_flow


def enrich_userinfo(monkeypatch, preferences):
    """Keep the original signed provider wire and enrich only UserInfo JSON."""
    original = ssrf_proxy.make_request_with_deadline

    def transport(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if urlsplit(url).path.endswith("userinfo"):
            return response(result.json() | preferences)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)


def complete_and_read_preferences(flow, scope):
    result = complete(flow, scope)
    assert (
        result.status == 302
        and result.error is None
        and type(result.tokens) is AuthTokenPair
    )
    assert result.redirect == flow.settings.CONSOLE_WEB_URL + "/apps/preferences"
    assert (
        result.phases.local_outcome,
        result.phases.finalization_outcome,
        result.phases.token_outcome,
        result.phases.cleanup_released,
    ) == ("committed", "committed", "issued", True)
    assert len(flow.control.created) == len(flow.control.consumed) == 1
    assert flow.control.consumed[0].nonce == flow.control.created[0].nonce
    assert (
        len(
            [path for path, _ in flow.control.requests if path.endswith("access_token")]
        )
        == 1
    )
    assert (
        len([call for call in flow.init.calls if call[0] == auth.CONSUME_SCRIPT]) == 1
    )
    assert len(flow.control.tokens) == 2 and not flow.lease.data
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    assert counts(flow) == {
        Account: 1,
        AccountMoneyExtend: 1,
        CasdoorIdentityExtend: 1,
        TenantAccountJoin: 2,
    }
    claims = jwt.decode(
        result.tokens.access_token, dify_config.SECRET_KEY, algorithms=["HS256"]
    )
    assert claims["sub"] == "Console API Passport" and result.tokens.csrf_token
    assert len(result.tokens.refresh_token) == 128
    with Session(flow.local.engine) as reader:
        account = reader.get(Account, claims["user_id"])
        assert account.status is AccountStatus.ACTIVE and account.initialized_at
        identity = reader.scalar(sa.select(CasdoorIdentityExtend))
        assert identity.account_id == account.id and identity.sync_generation == 1
        assert set(
            reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))
        ) == {
            CasdoorFinalizationState.FINALIZED,
        }
        return account.interface_language, account.timezone


def test_request_language_selects_original_zone_mapping_when_idp_zone_is_invalid(
    http_flow, monkeypatch
):
    from constants.languages import language_timezone_mapping

    flow = http_flow
    enrich_userinfo(monkeypatch, {"locale": "ja-JP", "zoneinfo": "Invalid/Zone"})
    scope, _ = begin(flow, return_path="/apps/preferences", locale="zh-Hans")

    assert complete_and_read_preferences(flow, scope) == (
        "zh-Hans",
        language_timezone_mapping["zh-Hans"],
    )
    assert (
        flow.control.consumed[0].context.locale,
        flow.control.consumed[0].context.timezone,
    ) == (
        "zh-Hans",
        None,
    )


def test_idp_language_selects_original_zone_mapping_when_no_zone_is_supplied(
    http_flow, monkeypatch
):
    from constants.languages import language_timezone_mapping

    flow = http_flow
    enrich_userinfo(monkeypatch, {"locale": "ja-JP"})
    scope, _ = begin(flow, return_path="/apps/preferences")

    assert complete_and_read_preferences(flow, scope) == (
        "ja-JP",
        language_timezone_mapping["ja-JP"],
    )
    assert (
        flow.control.consumed[0].context.locale,
        flow.control.consumed[0].context.timezone,
    ) == (None, None)
