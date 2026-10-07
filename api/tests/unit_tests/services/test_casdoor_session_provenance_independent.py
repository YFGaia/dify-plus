"""Independent full-factory tests for D08 refresh/logout composition."""

from http.cookies import SimpleCookie

from libs.token import _real_cookie_name
from test_casdoor_session_provenance_extend import mounted_get, mounted_normal_login

pytest_plugins = ("test_casdoor_session_provenance_extend",)


def _install_original_redis(d):
    """Give only the factory's external Redis wire a deterministic key store."""
    store = {}
    redis = d.f.redis
    for key, _ttl, value in d.f.control.tokens:
        store[key] = str(value).encode()

    def get(key):
        return store.get(key)

    def setex(key, _ttl, value):
        store[key] = str(value).encode()
        # The app's production composition has a separate private-runtime test wire;
        # mirror actual original refresh mappings as the shared Redis would.
        d.wire.setex(key, _ttl, value)

    def delete(*keys):
        for key in keys:
            store.pop(key, None)
            d.wire.delete(key)

    redis.get = get
    redis.setex = setex
    redis.delete = delete
    # The fixture copies the initial pair into its private wire before each GET.
    # Disable only that fixture bootstrap after the real callback so later tests
    # can model another browser advancing the same-account latest index.
    d.runtime.before_open = lambda: None
    return store


def _account_id(d):
    key = next(key for key, _ttl, _value in d.f.control.tokens if str(key).startswith("account_refresh_token:"))
    return str(key).rsplit(":", 1)[1]


def _cookie_headers(response):
    jar = SimpleCookie()
    for header in response.headers.getlist("Set-Cookie"):
        jar.load(header)
    return jar


def test_actual_factory_session_uses_current_request_refresh_not_latest_index(factory_login):
    d = factory_login
    mounted_normal_login(d)
    _install_original_redis(d)
    refresh_cookie = _real_cookie_name("refresh_token")
    current = d.client.get_cookie(refresh_cookie, domain="console.example.test").value
    other = "same-account-other-browser-valid-refresh"
    d.f.redis.setex("refresh_token:" + other, 180, _account_id(d))
    d.f.redis.setex("account_refresh_token:" + _account_id(d), 180, other)
    d.wire.setex("account_refresh_token:" + _account_id(d), 180, other)
    result = mounted_get(d, "/console/api/auth/casdoor/session")
    assert result.status_code == 200
    assert result.json["source"] == "casdoor"
    assert d.wire.get("refresh_token:" + current) == _account_id(d).encode()


def test_actual_factory_refresh_cas_failure_delivers_rotated_pair_and_clears_source_cookie(factory_login):
    d = factory_login
    mounted_normal_login(d)
    store = _install_original_redis(d)
    refresh_cookie = _real_cookie_name("refresh_token")
    old = d.client.get_cookie(refresh_cookie, domain="console.example.test").value
    d.wire.fail_cas = True
    response = d.client.post(
        "/console/api/refresh-token",
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
    )
    assert response.status_code == 200
    assert response.json == {"result": "success"}
    jar = _cookie_headers(response)
    assert jar[_real_cookie_name("access_token")].value
    rotated = jar[refresh_cookie].value
    assert rotated != old
    assert store["refresh_token:" + rotated] == _account_id(d).encode()
    assert jar[d.services.casdoor_session.cookie_name()]["max-age"] == "0"
    assert d.wire.get("refresh_token:" + rotated) == _account_id(d).encode()


def test_actual_factory_logout_clears_all_cookies_but_preserves_legacy_latest_only_revoke(factory_login, monkeypatch):
    d = factory_login
    mounted_normal_login(d)
    store = _install_original_redis(d)
    refresh_cookie = _real_cookie_name("refresh_token")
    request_refresh = d.client.get_cookie(refresh_cookie, domain="console.example.test").value
    other = "same-account-other-browser-valid-refresh"
    d.f.redis.setex("refresh_token:" + other, 180, _account_id(d))
    d.f.redis.setex("account_refresh_token:" + _account_id(d), 180, other)
    d.wire.setex("account_refresh_token:" + _account_id(d), 180, other)
    before = mounted_get(d, "/console/api/auth/casdoor/session")
    assert before.json["source"] == "casdoor"
    from controllers.console import wraps

    monkeypatch.setattr(wraps, "_is_setup_completed", lambda: True)
    response = d.client.post(
        "/console/api/logout",
        base_url=d.f.settings.CONSOLE_API_URL,
        environ_overrides={"REMOTE_ADDR": "192.0.2.7"},
    )
    assert response.status_code == 200
    assert response.json == {"result": "success"}
    jar = _cookie_headers(response)
    for name in (
        _real_cookie_name("access_token"),
        refresh_cookie,
        _real_cookie_name("csrf_token"),
        d.services.casdoor_session.cookie_name(),
        "__Host-casdoor_source",
    ):
        assert name in jar, name
        assert jar[name]["max-age"] == "0" or jar[name]["expires"], name
    assert store.get("refresh_token:" + request_refresh) == _account_id(d).encode()
    assert store.get("refresh_token:" + other) is None
    assert store.get("account_refresh_token:" + _account_id(d)) is None
