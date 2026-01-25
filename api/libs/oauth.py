import json
import urllib.parse
from dataclasses import dataclass

import httpx
import requests

from configs import dify_config  # Extend OAuto third-party login
from extensions.ext_database import db  # Extend OAuto third-party login
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend  # Extend OAuto third-party login


@dataclass
class OAuthUserInfo:
    id: str
    name: str
    email: str


class OAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(self):
        raise NotImplementedError()

    def get_access_token(self, code: str):
        raise NotImplementedError()

    def get_raw_user_info(self, token: str):
        raise NotImplementedError()

    def get_user_info(self, token: str) -> OAuthUserInfo:
        raw_info = self.get_raw_user_info(token)
        return self._transform_user_info(raw_info)

    def _transform_user_info(self, raw_info: dict) -> OAuthUserInfo:
        raise NotImplementedError()


class GitHubOAuth(OAuth):
    _AUTH_URL = "https://github.com/login/oauth/authorize"
    _TOKEN_URL = "https://github.com/login/oauth/access_token"
    _USER_INFO_URL = "https://api.github.com/user"
    _EMAIL_INFO_URL = "https://api.github.com/user/emails"

    def get_authorization_url(self, invite_token: str | None = None):
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "user:email",  # Request only basic user information
        }
        if invite_token:
            params["state"] = invite_token
        return f"{self._AUTH_URL}?{urllib.parse.urlencode(params)}"

    def get_access_token(self, code: str):
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Accept": "application/json"}
        response = httpx.post(self._TOKEN_URL, data=data, headers=headers)

        return response.json()

    def get_raw_user_info(self, token: str):
        headers = {"Authorization": f"token {token}"}
        response = httpx.get(self._USER_INFO_URL, headers=headers)
        response.raise_for_status()
        user_info = response.json()

        email_response = httpx.get(self._EMAIL_INFO_URL, headers=headers)
        email_info = email_response.json()
        primary_email: dict = next((email for email in email_info if email["primary"] == True), {})

        return {**user_info, "email": primary_email.get("email", "")}

    def _transform_user_info(self, raw_info: dict) -> OAuthUserInfo:
        email = raw_info.get("email")
        if not email:
            email = f"{raw_info['id']}+{raw_info['login']}@users.noreply.github.com"
        return OAuthUserInfo(id=str(raw_info["id"]), name=raw_info["name"], email=email)


class GoogleOAuth(OAuth):
    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _USER_INFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    def get_authorization_url(self, invite_token: str | None = None):
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": "openid email",
        }
        if invite_token:
            params["state"] = invite_token
        return f"{self._AUTH_URL}?{urllib.parse.urlencode(params)}"

    def get_access_token(self, code: str):
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Accept": "application/json"}
        response = httpx.post(self._TOKEN_URL, data=data, headers=headers)

        return response.json()

    def get_raw_user_info(self, token: str):
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.get(self._USER_INFO_URL, headers=headers)
        response.raise_for_status()
        return response.json()

    def _transform_user_info(self, raw_info: dict) -> OAuthUserInfo:
        return OAuthUserInfo(id=str(raw_info["sub"]), name="", email=raw_info["email"])


# Extend Start: OAuth2
class OaOAuth(OAuth):

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
        server_url = config.get('server_url') or ''
        authorize_url = config.get('authorize_url') or ''
        token_url = config.get('token_url') or ''
        userinfo_url = config.get('userinfo_url') or ''
        discovery_url = config.get('discovery_url') or ''

        # If any endpoint missing and discovery available, fetch
        if discovery_url and (not authorize_url or not token_url or not userinfo_url):
            try:
                discover_full = self._join_url(server_url, discovery_url)
                resp = requests.get(discover_full, timeout=10)
                if resp.ok:
                    data = resp.json()
                    authorize_url = authorize_url or data.get('authorization_endpoint', '')
                    token_url = token_url or data.get('token_endpoint', '')
                    userinfo_url = userinfo_url or data.get('userinfo_endpoint', '')
            except Exception:
                pass

        return {
            'authorize_url': self._join_url(server_url, authorize_url),
            'token_url': self._join_url(server_url, token_url),
            'userinfo_url': self._join_url(server_url, userinfo_url),
        }

    def get_auto2_conf(self):
        # oauth start
        integration = db.session.query(SystemIntegrationExtend).filter(
            SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_OAUTH_TWO).first()
        if integration is None or (integration and not integration.status):
            return {
                "integration": integration,
                "config": {},
                "passwd": ""
            }
        return {
            "integration": integration,
            "passwd": integration.decodeSecret(),
            "config": json.loads(integration.config)
        }

    def extract_data(self, dictionary, path):
        """
        从字典中提取指定路径的数据
        支持通配符'*'获取列表中所有元素的特定字段

        Args:
            dictionary (dict): 源字典
            path (str): 以点分隔的路径，如 "data.info.name" 或 "data.items.*.name"

        Returns:
            提取的数据
        """
        parts = path.split('.')
        current = dictionary

        for i, part in enumerate(parts):
            if part == '*' and isinstance(current, list):
                # 处理列表中的每个元素
                remainder = '.'.join(parts[i + 1:])
                if remainder:
                    return [self.extract_data(item, remainder) for item in current]
                else:
                    return current
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def get_authorization_url(self, invite_token: str | None = None):
        auto2_conf = self.get_auto2_conf()
        integration = auto2_conf.get('integration')
        if integration is None:
            return
        # 构建查询参数
        config = auto2_conf.get('config')
        params = {
            'response_type': 'code',
            'redirect_uri': dify_config.CONSOLE_API_URL + "/console/api/oauth/authorize/oauth2",
            'client_id': integration.app_id,
            'scope': config.get('scope'),
        }
        if invite_token:
            params['state'] = invite_token
        query_string = urllib.parse.urlencode(params)

        endpoints = self._resolve_endpoints(config)
        auth_url = endpoints.get('authorize_url')
        return f"{auth_url}{'&' if "?" in auth_url else '?'}{query_string}"

    def get_access_token(self, code: str):
        auto2_conf = self.get_auto2_conf()
        integration = auto2_conf.get('integration')
        if integration is None:
            return ""
        config = auto2_conf.get('config')
        endpoints = self._resolve_endpoints(config)
        token_url = endpoints.get('token_url')
        token_auth_method = str(config.get('token_auth_method') or '').strip().lower()
        use_basic = token_auth_method == 'client_secret_basic'

        # 构建请求
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": dify_config.CONSOLE_API_URL + "/console/api/oauth/authorize/oauth2",
        }
        headers = {"Accept": "application/json"}
        if use_basic:
            auth = (integration.app_id, auto2_conf.get('passwd'))
        else:
            data.update({
                "client_id": integration.app_id,
                "client_secret": auto2_conf.get('passwd'),
            })
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
        if auto2_conf.get('integration') is None:
            return ""
        config = auto2_conf.get('config')
        endpoints = self._resolve_endpoints(config)

        # 检查token是否为空
        if not token or token.strip() == "":
            raise ValueError("OAuth2 access token is empty or invalid")

        # 尝试不同的Authorization header格式
        auth_formats = [
            f"Bearer {token}",
            f"Token {token}",
            token
        ]

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
        if not raw_info or not isinstance(raw_info, dict) or auto2_conf.get('integration') is None:
            return OAuthUserInfo(
                id="",
                name="",
                email="",
            )
        # 提取参数（更健壮：支持点分路径、扁平键名和标准 OIDC 兜底）
        config = auto2_conf.get('config')
        name_field = config.get('user_name_field') if isinstance(config, dict) else None
        email_field = config.get('user_email_field') if isinstance(config, dict) else None
        id_field = config.get('user_id_field') if isinstance(config, dict) else None

        # 首选：按配置路径提取
        name = self.extract_data(raw_info, name_field) if name_field else None
        email = self.extract_data(raw_info, email_field) if email_field else None
        username = self.extract_data(raw_info, id_field) if id_field else None

        # 如果配置为 data.name 但返回是扁平结构，尝试最后一级键名
        if name is None and isinstance(name_field, str) and '.' in name_field:
            name = raw_info.get(name_field.split('.')[-1])
        if email is None and isinstance(email_field, str) and '.' in email_field:
            email = raw_info.get(email_field.split('.')[-1])
        if username is None and isinstance(id_field, str) and '.' in id_field:
            username = raw_info.get(id_field.split('.')[-1])

        # OIDC 常见字段兜底
        if username is None:
            username = raw_info.get('sub') or raw_info.get('preferred_username') or raw_info.get('id') or raw_info.get('user_id')
        if name is None:
            name = raw_info.get('name') or raw_info.get('preferred_username')
        if email is None:
            email = raw_info.get('email')

        if not username:
            raise ValueError("OAuth2返回用户数据格式不正确。请检查相关配置是否正确。响应信息为：" + str(raw_info))

        return OAuthUserInfo(
            id=str(username) if username is not None else None,
            name=str(name) if name is not None else None,
            email=str(email) if email is not None else None,
        )
# Extend Stop: OAuth
