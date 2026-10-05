"""Independent mounted RP logout source-revocation regression; synthetic offline IdP."""

from urllib.parse import parse_qs, urlsplit

from services.casdoor_rp_logout_service_extend import RP_CALLBACK_PATH, RP_HANDOFF_PATH
from test_casdoor_diagnostic_flow_extend import send
from test_casdoor_rp_logout_mounted_extend import MANAGEMENT, normal_session

pytest_plugins = (
    "test_casdoor_rp_logout_service_extend",
    "test_casdoor_diagnostic_flow_extend",
    "test_casdoor_rp_logout_mounted_extend",
)


def test_explicit_policy_revocation_after_local_logout_blocks_provider_navigation(
    mounted, monkeypatch
):
    m, d = mounted, mounted.d
    services, _ = normal_session(m, monkeypatch)
    from libs.token import _real_cookie_name

    csrf = d.client.get_cookie(
        _real_cookie_name("csrf_token"), domain="console.example.test"
    ).value
    logout = d.client.post(
        "/console/api/logout",
        headers={"X-CSRF-Token": csrf},
        base_url=d.f.settings.CONSOLE_API_URL,
    )
    assert logout.status_code == 200
    assert logout.json["result"] == "success"
    assert "casdoor_logout" in logout.json, (
        "a live native snapshot must reach the original local logout before optional authority is rechecked; "
        f"the local logout response was {logout.json!r}"
    )
    assert logout.json["casdoor_logout"]["status"] == "handoff_ready"
    handoff = logout.json["casdoor_logout"]["handoff"]["handoff_path"]
    assert handoff.startswith(RP_HANDOFF_PATH)
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test") is None

    provider_requests_before = tuple(d.f.control.requests)
    m.rp.authority.unlink()
    unavailable = send(d, handoff)

    assert unavailable.status_code == 303
    assert unavailable.location.endswith("/signin/casdoor-logout?status=unavailable")
    assert m.rp.config.expected_issuer not in unavailable.location
    assert "id_token" not in unavailable.location
    assert unavailable.headers["Cache-Control"] == "no-store"
    assert unavailable.headers["Referrer-Policy"] == "no-referrer"
    assert tuple(d.f.control.requests) == provider_requests_before
    assert services.casdoor_rp_logout.observation(m.rp.binding) is None


def test_actual_disable_preserves_previously_trusted_snapshot_for_local_first_logout(mounted, monkeypatch):
    """The exact old policy remains usable after the real manager disable route."""
    m, d = mounted, mounted.d
    manager_client = d.app.test_client()
    from libs.token import _real_cookie_name

    for name, value in d.original_cookies.items():
        manager_client.set_cookie(_real_cookie_name(name), value, domain="console.example.test", path="/")
    normal_session(m, monkeypatch)

    before = d.services.casdoor_configuration.get(d.actor)
    disabled = send(
        d,
        MANAGEMENT + "/disable",
        method="POST",
        client=manager_client,
        json={"etag": before.etag},
    )
    assert disabled.status_code == 200, disabled.json
    assert disabled.json["configuration"]["enabled"] is False

    csrf = d.client.get_cookie(
        _real_cookie_name("csrf_token"), domain="console.example.test"
    ).value
    logout = d.client.post(
        "/console/api/logout",
        headers={"X-CSRF-Token": csrf},
        base_url=d.f.settings.CONSOLE_API_URL,
    )
    assert logout.status_code == 200
    assert logout.json["result"] == "success"
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test") is None
    assert "casdoor_logout" in logout.json, (
        "a live native snapshot accepted before disable must remain usable for RP logout; "
        f"the local logout response was {logout.json!r}"
    )
    assert logout.json["casdoor_logout"]["status"] == "handoff_ready"
    handoff = logout.json["casdoor_logout"]["handoff"]["handoff_path"]
    assert handoff.startswith(RP_HANDOFF_PATH)

    provider = send(d, handoff)
    assert provider.status_code == 303
    assert provider.location.startswith(m.rp.config.expected_issuer + "/api/logout?")
    query = parse_qs(urlsplit(provider.location).query)
    assert set(query) == {"id_token_hint", "post_logout_redirect_uri", "state"}
    assert query["post_logout_redirect_uri"] == [m.rp.manifest["rp_logout"]["post_logout_redirect_uri"]]

    returned = send(d, RP_CALLBACK_PATH, query_string={"state": query["state"][0]})
    assert returned.status_code == 303
    assert returned.location.endswith("/signin/casdoor-logout?status=returned")
    assert "id_token" not in returned.location
    for name in ("access_token", "refresh_token"):
        assert d.client.get_cookie(_real_cookie_name(name), domain="console.example.test") is None
