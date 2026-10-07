import base64
import binascii
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import NotRequired, TypedDict, override

import httpx
import requests
from pydantic import TypeAdapter, ValidationError

from configs import dify_config  # Extend OAuto third-party login
from core.helper.http_client_pooling import get_pooled_http_client
from extensions.ext_database import db  # Extend OAuto third-party login
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend  # Extend OAuto third-party login

logger = logging.getLogger(__name__)

type JsonObject = dict[str, object]
type JsonObjectList = list[JsonObject]

JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
JSON_OBJECT_LIST_ADAPTER: TypeAdapter[JsonObjectList] = TypeAdapter(JsonObjectList)

# Reuse a pooled httpx.Client for OAuth flows (public endpoints, no SSRF proxy).
_http_client: httpx.Client = get_pooled_http_client(
    "oauth:default",
    lambda: httpx.Client(limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)),
)


class AccessTokenResponse(TypedDict, total=False):
    access_token: str


class OAuthState(TypedDict, total=False):
    invite_token: str
    timezone: str
    language: str
    redirect_url: str


class GitHubEmailRecord(TypedDict, total=False):
    email: str
    primary: bool
    verified: bool


class GitHubRawUserInfo(TypedDict):
    id: int | str
    login: str
    name: NotRequired[str | None]
    email: NotRequired[str | None]


class GoogleRawUserInfo(TypedDict):
    sub: str
    email: str


ACCESS_TOKEN_RESPONSE_ADAPTER = TypeAdapter(AccessTokenResponse)
OAUTH_STATE_ADAPTER = TypeAdapter(OAuthState)
GITHUB_RAW_USER_INFO_ADAPTER = TypeAdapter(GitHubRawUserInfo)
GITHUB_EMAIL_RECORDS_ADAPTER = TypeAdapter(list[GitHubEmailRecord])
GOOGLE_RAW_USER_INFO_ADAPTER = TypeAdapter(GoogleRawUserInfo)


@dataclass
class OAuthUserInfo:
    id: str
    name: str
    email: str


def encode_oauth_state(
    invite_token: str | None = None,
    timezone: str | None = None,
    language: str | None = None,
    redirect_url: str | None = None,
) -> str | None:
    state: OAuthState = {}
    if invite_token:
        state["invite_token"] = invite_token
    if timezone:
        state["timezone"] = timezone
    if language:
        state["language"] = language
    if redirect_url:
        state["redirect_url"] = redirect_url
    if not state:
        return None

    raw_state = json.dumps(state, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_state).decode("ascii").rstrip("=")


def decode_oauth_state(state: str | None) -> OAuthState:
    if not state:
        return {}

    try:
        padded_state = state + "=" * (-len(state) % 4)
        raw_state = base64.urlsafe_b64decode(padded_state.encode("ascii")).decode("utf-8")
        return OAUTH_STATE_ADAPTER.validate_python(json.loads(raw_state))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return {}


def _json_object(response: httpx.Response) -> JsonObject:
    return JSON_OBJECT_ADAPTER.validate_python(response.json())


def _json_list(response: httpx.Response) -> JsonObjectList:
    return JSON_OBJECT_LIST_ADAPTER.validate_python(response.json())


class OAuth:
    client_id: str
    client_secret: str
    redirect_uri: str

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(
        self,
        invite_token: str | None = None,
        timezone: str | None = None,
        language: str | None = None,
        redirect_url: str | None = None,
    ) -> str:
        raise NotImplementedError()

    def get_access_token(self, code: str) -> str:
        raise NotImplementedError()

    def get_raw_user_info(self, token: str) -> JsonObject:
        raise NotImplementedError()

    def get_user_info(self, token: str) -> OAuthUserInfo:
        raw_info = self.get_raw_user_info(token)
        return self._transform_user_info(raw_info)

    def _transform_user_info(self, raw_info: JsonObject) -> OAuthUserInfo:
        raise NotImplementedError()


class GitHubOAuth(OAuth):
    _AUTH_URL = "https://github.com/login/oauth/authorize"
    _TOKEN_URL = "https://github.com/login/oauth/access_token"
    _USER_INFO_URL = "https://api.github.com/user"
    _EMAIL_INFO_URL = "https://api.github.com/user/emails"

    @override
    def get_authorization_url(
        self,
        invite_token: str | None = None,
        timezone: str | None = None,
        language: str | None = None,
        redirect_url: str | None = None,
    ) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "user:email",  # Request only basic user information
        }
        state = encode_oauth_state(
            invite_token=invite_token,
            timezone=timezone,
            language=language,
            redirect_url=redirect_url,
        )
        if state:
            params["state"] = state
        return f"{self._AUTH_URL}?{urllib.parse.urlencode(params)}"

    @override
    def get_access_token(self, code: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Accept": "application/json"}
        response = _http_client.post(self._TOKEN_URL, data=data, headers=headers)

        response_json = ACCESS_TOKEN_RESPONSE_ADAPTER.validate_python(_json_object(response))
        access_token = response_json.get("access_token")

        if not access_token:
            raise ValueError(f"Error in GitHub OAuth: {response_json}")

        return access_token

    @override
    def get_raw_user_info(self, token: str) -> JsonObject:
        headers = {"Authorization": f"token {token}"}
        response = _http_client.get(self._USER_INFO_URL, headers=headers)
        response.raise_for_status()
        user_info = GITHUB_RAW_USER_INFO_ADAPTER.validate_python(_json_object(response))

        # Only call the /user/emails endpoint when the profile email is absent,
        # i.e. the user has "Keep my email addresses private" enabled.
        resolved_email = user_info.get("email") or ""
        if not resolved_email:
            resolved_email = self._get_email_from_emails_endpoint(headers)

        return {**user_info, "email": resolved_email}

    @staticmethod
    def _get_email_from_emails_endpoint(headers: dict[str, str]) -> str:
        """Fetch the best available email from GitHub's /user/emails endpoint.

        Prefers the primary email, then falls back to any verified email.
        Returns an empty string when no usable email is found.
        """
        try:
            email_response = _http_client.get(GitHubOAuth._EMAIL_INFO_URL, headers=headers)
            email_response.raise_for_status()
            email_records = GITHUB_EMAIL_RECORDS_ADAPTER.validate_python(_json_list(email_response))
        except (httpx.HTTPStatusError, ValidationError):
            logger.warning("Failed to retrieve email from GitHub /user/emails endpoint", exc_info=True)
            return ""

        primary = next((r for r in email_records if r.get("primary") is True), None)
        if primary:
            return primary.get("email", "")

        # No primary email; try any verified email as a fallback.
        verified = next((r for r in email_records if r.get("verified") is True), None)
        if verified:
            return verified.get("email", "")

        return ""

    @override
    def _transform_user_info(self, raw_info: JsonObject) -> OAuthUserInfo:
        payload = GITHUB_RAW_USER_INFO_ADAPTER.validate_python(raw_info)
        email = payload.get("email") or ""
        if not email:
            # When no email is available from the profile or /user/emails endpoint,
            # fall back to GitHub's noreply address so sign-in can still proceed.
            # Use only the numeric ID (not the login) so the address stays stable
            # even if the user renames their GitHub account.
            github_id = payload["id"]
            email = f"{github_id}@users.noreply.github.com"
            logger.info("GitHub user %s has no public email; using noreply address", payload["login"])
        return OAuthUserInfo(id=str(payload["id"]), name=str(payload.get("name") or ""), email=email)


class GoogleOAuth(OAuth):
    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _USER_INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    @override
    def get_authorization_url(
        self,
        invite_token: str | None = None,
        timezone: str | None = None,
        language: str | None = None,
        redirect_url: str | None = None,
    ) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": "openid email",
        }
        state = encode_oauth_state(
            invite_token=invite_token,
            timezone=timezone,
            language=language,
            redirect_url=redirect_url,
        )
        if state:
            params["state"] = state
        return f"{self._AUTH_URL}?{urllib.parse.urlencode(params)}"

    @override
    def get_access_token(self, code: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Accept": "application/json"}
        response = _http_client.post(self._TOKEN_URL, data=data, headers=headers)

        response_json = ACCESS_TOKEN_RESPONSE_ADAPTER.validate_python(_json_object(response))
        access_token = response_json.get("access_token")

        if not access_token:
            raise ValueError(f"Error in Google OAuth: {response_json}")

        return access_token

    @override
    def get_raw_user_info(self, token: str) -> JsonObject:
        headers = {"Authorization": f"Bearer {token}"}
        response = _http_client.get(self._USER_INFO_URL, headers=headers)
        response.raise_for_status()
        return _json_object(response)

    @override
    def _transform_user_info(self, raw_info: JsonObject) -> OAuthUserInfo:
        payload = GOOGLE_RAW_USER_INFO_ADAPTER.validate_python(raw_info)
        return OAuthUserInfo(id=str(payload["sub"]), name="", email=payload["email"])


# Extend Start: OAuth2
class OaOAuth(OAuth):
    def _resolve_redirect_uri(self, config: dict) -> str:
        """Use the configured callback for both OAuth requests, preserving the legacy default."""
        redirect_uri = config.get("redirect_uri")
        if redirect_uri is None or (isinstance(redirect_uri, str) and not redirect_uri.strip()):
            return dify_config.CONSOLE_API_URL.rstrip("/") + "/console/api/oauth/authorize/oauth2"
        if not isinstance(redirect_uri, str):
            raise ValueError("OAuth2 redirect_uri must be an absolute HTTP(S) URL without a fragment")
        redirect_uri = redirect_uri.strip()
        parsed = urllib.parse.urlsplit(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or "#" in redirect_uri:
            raise ValueError("OAuth2 redirect_uri must be an absolute HTTP(S) URL without a fragment")
        return redirect_uri

    def _is_absolute_url(self, url: str) -> bool:
        return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))

    def _join_url(self, base: str, path_or_url: str) -> str:
        if not path_or_url:
            return ""
        if self._is_absolute_url(path_or_url):
            return path_or_url
        return f"{base}{path_or_url}"

    def _resolve_endpoints(self, config: dict) -> dict:
        """
        Resolve authorize/token/userinfo endpoints from config or OIDC discovery.
        """
        if not isinstance(config, dict):
            return {}
        server_url = config.get("server_url") or ""
        authorize_url = config.get("authorize_url") or ""
        token_url = config.get("token_url") or ""
        userinfo_url = config.get("userinfo_url") or ""
        discovery_url = config.get("discovery_url") or ""

        # If any endpoint missing and discovery available, fetch
        if discovery_url and (not authorize_url or not token_url or not userinfo_url):
            try:
                discover_full = self._join_url(server_url, discovery_url)
                resp = requests.get(discover_full, timeout=10)
                if resp.ok:
                    data = resp.json()
                    authorize_url = authorize_url or data.get("authorization_endpoint", "")
                    token_url = token_url or data.get("token_endpoint", "")
                    userinfo_url = userinfo_url or data.get("userinfo_endpoint", "")
            except Exception:
                # 发现端点失败时静默回退到已配置的端点，不中断 OAuth 流程
                logger.debug("Failed to fetch OIDC discovery document from %s", discovery_url, exc_info=True)

        return {
            "authorize_url": self._join_url(server_url, authorize_url),
            "token_url": self._join_url(server_url, token_url),
            "userinfo_url": self._join_url(server_url, userinfo_url),
        }

    def get_auto2_conf(self):
        # oauth start
        integration = (
            db.session.query(SystemIntegrationExtend)
            .filter(SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_OAUTH_TWO)
            .first()
        )
        if integration is None or (integration and not integration.status):
            return {"integration": integration, "config": {}, "passwd": ""}
        return {
            "integration": integration,
            "passwd": integration.decodeSecret(),
            "config": json.loads(integration.config),
        }

    def _normalize_jinja_path(self, path: str) -> str:
        """
        规范化 Jinja 风格路径：去掉 {{ }} 及首尾空格，得到点分路径供 extract_data 使用。
        例如 "{{ user.name }}" -> "user.name"，"email" -> "email"。
        """
        if not path or not isinstance(path, str):
            return ""
        s = path.strip().replace("{{", "").replace("}}", "").strip()
        return s

    def extract_data(self, dictionary, path):
        """
        从字典中提取指定路径的数据
        支持通配符'*'获取列表中所有元素的特定字段；路径可为 Jinja 风格（调用前用 _normalize_jinja_path 规范化）。

        Args:
            dictionary (dict): 源字典
            path (str): 以点分隔的路径，如 "data.info.name" 或 "data.items.*.name"

        Returns:
            提取的数据
        """
        if not path:
            return None
        parts = path.split(".")
        current = dictionary

        for i, part in enumerate(parts):
            if part == "*" and isinstance(current, list):
                # 处理列表中的每个元素
                remainder = ".".join(parts[i + 1 :])
                if remainder:
                    return [self.extract_data(item, remainder) for item in current]
                else:
                    return current
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def get_authorization_url(
        self,
        invite_token: str | None = None,
        timezone: str | None = None,
        language: str | None = None,
        redirect_url: str | None = None,
    ):
        auto2_conf = self.get_auto2_conf()
        integration = auto2_conf.get("integration")
        if integration is None:
            return
        # 构建查询参数
        config = auto2_conf.get("config")
        params = {
            "response_type": "code",
            "redirect_uri": self._resolve_redirect_uri(config),
            "client_id": integration.app_id,
            "scope": config.get("scope"),
        }
        # 上游 1.14.2 起 callback 用 decode_oauth_state 解包 state（base64-JSON），
        # 这里必须用 encode_oauth_state 编码，否则 invite_token 会在回调侧丢失；
        # 1.16.0 起 state 里额外携带 redirect_url，callback 侧做同源校验后回跳
        state = encode_oauth_state(
            invite_token=invite_token, timezone=timezone, language=language, redirect_url=redirect_url
        )
        if state:
            params["state"] = state
        query_string = urllib.parse.urlencode(params)

        endpoints = self._resolve_endpoints(config)
        auth_url = endpoints.get("authorize_url")
        return f"{auth_url}{'&' if '?' in auth_url else '?'}{query_string}"

    def get_access_token(self, code: str):
        auto2_conf = self.get_auto2_conf()
        integration = auto2_conf.get("integration")
        if integration is None:
            return ""
        config = auto2_conf.get("config")
        endpoints = self._resolve_endpoints(config)
        token_url = endpoints.get("token_url")
        token_auth_method = str(config.get("token_auth_method") or "").strip().lower()
        use_basic = token_auth_method == "client_secret_basic"

        # 构建请求
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._resolve_redirect_uri(config),
        }
        headers = {"Accept": "application/json"}
        if use_basic:
            auth = (integration.app_id, auto2_conf.get("passwd"))
        else:
            data.update(
                {
                    "client_id": integration.app_id,
                    "client_secret": auto2_conf.get("passwd"),
                }
            )
            auth = None

        if not code:
            return ""

        response = requests.post(token_url, data=data, headers=headers, auth=auth, timeout=30)
        response.encoding = "utf-8"
        if response.status_code != 200:
            return ""

        return response.json()

    def get_raw_user_info(self, token: str):
        auto2_conf = self.get_auto2_conf()
        if auto2_conf.get("integration") is None:
            return ""
        config = auto2_conf.get("config")
        endpoints = self._resolve_endpoints(config)

        # 检查token是否为空
        if not token or token.strip() == "":
            raise ValueError("OAuth2 access token is empty or invalid")

        # 尝试不同的Authorization header格式
        auth_formats = [f"Bearer {token}", f"Token {token}", token]

        last_error = None
        for auth_header in auth_formats:
            try:
                headers = {"Authorization": auth_header}
                response = requests.get(f"{endpoints.get('userinfo_url')}", headers=headers, timeout=30)

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 401:
                    last_error = f"401 Unauthorized: {response.text}"
                    continue
                else:
                    last_error = f"HTTP {response.status_code}: {response.text}"
                    continue

            except requests.RequestException as e:
                last_error = str(e)
                continue

        # 如果所有格式都失败，抛出最后一个错误
        if last_error:
            raise requests.RequestException(f"All authentication formats failed. Last error: {last_error}")
        else:
            raise requests.RequestException("Failed to get user info with any authentication format")

    def _transform_user_info(self, raw_info: dict) -> OAuthUserInfo:

        # 检查 raw_info 是否为空或为 None
        auto2_conf = self.get_auto2_conf()
        if not raw_info or not isinstance(raw_info, dict) or auto2_conf.get("integration") is None:
            return OAuthUserInfo(
                id="",
                name="",
                email="",
            )
        # 提取参数（支持 Jinja 风格路径如 name、user.name、{{ data.attributes.phone }}，及标准 OIDC 兜底）
        config = auto2_conf.get("config")
        name_field = config.get("user_name_field") if isinstance(config, dict) else None
        email_field = config.get("user_email_field") if isinstance(config, dict) else None
        id_field = config.get("user_id_field") if isinstance(config, dict) else None

        # 首选：按配置路径提取（路径会先做 Jinja 规范化：去掉 {{ }} 再按点分路径取）
        name_path = self._normalize_jinja_path(name_field) if name_field else ""
        email_path = self._normalize_jinja_path(email_field) if email_field else ""
        id_path = self._normalize_jinja_path(id_field) if id_field else ""

        name = self.extract_data(raw_info, name_path) if name_path else None
        email = self.extract_data(raw_info, email_path) if email_path else None
        username = self.extract_data(raw_info, id_path) if id_path else None

        # 如果配置为 data.name 但返回是扁平结构，尝试最后一级键名
        if name is None and name_path and "." in name_path:
            name = raw_info.get(name_path.split(".")[-1])
        if email is None and email_path and "." in email_path:
            email = raw_info.get(email_path.split(".")[-1])
        if username is None and id_path and "." in id_path:
            username = raw_info.get(id_path.split(".")[-1])

        # OIDC 常见字段兜底
        if username is None:
            username = (
                raw_info.get("sub")
                or raw_info.get("preferred_username")
                or raw_info.get("id")
                or raw_info.get("user_id")
            )
        if name is None:
            name = raw_info.get("name") or raw_info.get("preferred_username")
        if email is None:
            email = raw_info.get("email")

        if not username:
            raise ValueError("OAuth2返回用户数据格式不正确。请检查相关配置是否正确。响应信息为：" + str(raw_info))

        return OAuthUserInfo(
            id=str(username) if username is not None else None,
            name=str(name) if name is not None else None,
            email=str(email) if email is not None else None,
        )


# Extend Stop: OAuth
