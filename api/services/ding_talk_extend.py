"""DingTalk login integration helpers.

The DingTalk endpoints are fork-specific optional integrations. Missing DingTalk
SDK packages must not prevent the core API, workers, or workflow execution from
starting, because most deployments do not enable this login path.
"""

import json
import logging
import secrets
import time
from typing import Any

import requests
from flask import request

try:
    from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models
    from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util.client import Client as UtilClient
except ModuleNotFoundError as exc:
    dingtalkoauth_2__1__0_models = None
    # 命名与上方 SDK 导入别名保持一致，便于降级赋值，故豁免 mixedCase 检查
    dingtalkoauth2_1_0Client = None  # noqa: N816
    open_api_models = None
    UtilClient = None
    DINGTALK_SDK_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    DINGTALK_SDK_IMPORT_ERROR = None

try:
    from pypinyin import lazy_pinyin
except ModuleNotFoundError:
    lazy_pinyin = None

from configs import dify_config
from extensions.ext_database import db
from libs.helper import extract_remote_ip
from models.account import Account
from models.system_extend import SystemIntegrationClassify, SystemIntegrationExtend
from services.account_service import AccountService, RegisterService, TenantService
from services.account_service_extend import TenantExtendService

logger = logging.getLogger(__name__)
DINGTALK_ACCOUNT_TOKEN = {"time": 0, "token": ""}


class DingTalkService:
    @classmethod
    def _get_sdk_unavailable_error(cls) -> str:
        if DINGTALK_SDK_IMPORT_ERROR is None:
            return ""

        package_name = DINGTALK_SDK_IMPORT_ERROR.name or "alibabacloud_dingtalk"
        logger.warning("DingTalk SDK is unavailable: %s", DINGTALK_SDK_IMPORT_ERROR)
        return f"DingTalk integration dependency is not installed: {package_name}"

    @classmethod
    def create_client(cls) -> Any:
        """
        使用 Token 初始化账号Client
        @return: Client
        @throws Exception
        """
        dependency_error = cls._get_sdk_unavailable_error()
        if dependency_error:
            raise RuntimeError(dependency_error)

        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        return dingtalkoauth2_1_0Client(config)

    @classmethod
    def extract_data(cls, dictionary: dict, path: str):
        """
        从字典中提取指定路径的数据
        支持点号分隔的路径和数组索引

        Args:
            dictionary (dict): 源字典
            path (str): 以点分隔的路径，如 "data.email" 或 "data[0].userName"

        Returns:
            提取的数据，如果路径不存在返回None
        """
        if not path:
            return None

        import re

        # 处理路径中的数组索引，如 data[0].userName -> data.[0].userName
        path = re.sub(r"\[(\d+)\]", r".[\1]", path)
        parts = path.split(".")
        current = dictionary

        for part in parts:
            if not part:
                continue

            # 处理数组索引
            array_match = re.match(r"\[(\d+)\]", part)
            if array_match:
                index = int(array_match.group(1))
                if isinstance(current, list) and 0 <= index < len(current):
                    current = current[index]
                else:
                    return None
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    @classmethod
    def get_email_from_third_party_api(cls, userid: str, integration: SystemIntegrationExtend) -> str:
        """
        通过第三方API获取用户邮箱

        Args:
            userid: 钉钉用户ID
            integration: 集成配置对象

        Returns:
            邮箱地址，获取失败返回空字符串
        """
        from services.dingtalk_email_lookup_extend import lookup_email

        try:
            config = json.loads(integration.config or "{}").get("email_api", {})
            if not config.get("enabled", False):
                return ""
            result = lookup_email(userid, config)
            return result.get("email", "") if result["result"] == "success" else ""
        except Exception:
            logger.warning("Enterprise email lookup failed; using DingTalk email fallback")
            return ""

    @classmethod
    def get_user_token(cls, code: str) -> (str, str):
        dependency_error = cls._get_sdk_unavailable_error()
        if dependency_error:
            return "", dependency_error

        # get token
        client = cls.create_client()
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend)
            .filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK,
            )
            .first()
        )
        if integration is None:
            return "", "尚未配置钉钉登录"
        get_access_token_request = dingtalkoauth_2__1__0_models.GetUserTokenRequest(
            client_secret=integration.decodeSecret(),
            client_id=integration.app_key,
            grant_type="authorization_code",
            code=code,
        )
        #
        response = client.get_user_token(get_access_token_request)
        if response.status_code == 200:
            return response.body.access_token, ""
        else:
            return "", response.body

    @classmethod
    def get_access_token(cls) -> (str, str):
        global DINGTALK_ACCOUNT_TOKEN
        dependency_error = cls._get_sdk_unavailable_error()
        if dependency_error:
            return "", dependency_error

        if DINGTALK_ACCOUNT_TOKEN["time"] > time.time():
            return DINGTALK_ACCOUNT_TOKEN["token"], ""
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend)
            .filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK,
            )
            .first()
        )
        if integration is None:
            return "", "尚未配置钉钉登录"
        # get token
        client = cls.create_client()
        get_access_token_request = dingtalkoauth_2__1__0_models.GetAccessTokenRequest(
            app_secret=integration.decodeSecret(),
            app_key=integration.app_key,
        )
        try:
            token_request = client.get_access_token(get_access_token_request)
            if token_request.status_code == 200:
                DINGTALK_ACCOUNT_TOKEN["token"] = token_request.body.access_token
                DINGTALK_ACCOUNT_TOKEN["time"] = int(time.time()) + 3600
                return token_request.body.access_token, ""
            else:
                return "", token_request.body
        except Exception as err:
            if not UtilClient.empty(err.code) and not UtilClient.empty(err.message):
                # err 中含有 code 和 message 属性，可帮助开发定位问题
                return "", f"Failed to retrieve token:${err.code}, {err.message}"
            return "", "Failed to retrieve token"

    @classmethod
    def auto_create_user(cls, userid: str) -> (str, str):
        # 获取集成配置
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend)
            .filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK,
            )
            .first()
        )

        dingTalkToken, err = cls.get_access_token()
        responses = requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={dingTalkToken}",
            json={"userid": userid},
        )
        # Check the response status code
        if responses.status_code != 200:
            return "", f"Request for user information failed, status code: {responses.status_code}"
        reqs = responses.json()
        if reqs["errcode"] != 0:
            return "", "Request for user information failed: " + userid + " " + json.dumps(reqs)
        # Check if the user exists
        username = reqs["result"]["name"]

        # 优先尝试从第三方API获取邮箱
        email = ""
        if integration:
            email = cls.get_email_from_third_party_api(userid, integration)

        # 降级处理：使用钉钉返回的邮箱
        if not email and "email" in reqs["result"] and len(reqs["result"]["email"]):
            email = reqs["result"]["email"]

        # 最终降级：使用拼音生成邮箱
        if not email:
            if lazy_pinyin is not None:
                email = f"{''.join(lazy_pinyin(username))}@{dify_config.EMAIL_DOMAIN}"
                logger.info("Using pinyin-generated email for user %s: %s", userid, email)
            else:
                email = f"{userid}@{dify_config.EMAIL_DOMAIN}"
                logger.warning(
                    "pypinyin is unavailable, using DingTalk userid as email local part for %s",
                    userid,
                )

        account: Account = db.session.query(Account).filter(Account.email == email).first()
        if account is None:
            # registered user
            try:
                # generate random password
                new_password = secrets.token_urlsafe(16)
                account = RegisterService.register(
                    email=email,
                    name=username,
                    password=new_password,
                    language=dify_config.DEFAULT_LANGUAGE,
                    session=db.session(),
                )
            except EOFError as a:
                return "", f"register user error: {str(a)}， info {json.loads(reqs)}"

            tenant_extend_service = TenantExtendService
            super_admin_id = tenant_extend_service.get_super_admin_id().id
            super_admin_tenant_id = tenant_extend_service.get_super_admin_tenant_id().id
            if super_admin_id and super_admin_tenant_id:
                isCreate = TenantExtendService.create_default_tenant_member_if_not_exist(
                    super_admin_tenant_id, account.id
                )
                if isCreate:
                    # switch_tenant/login 自上游 1.16.0 起要求显式 session（keyword-only 无默认值），
                    # 此处沿用模块的全局 db.session（经 db.session() 取实体 Session）
                    TenantService.switch_tenant(account, super_admin_tenant_id, session=db.session())
        # token jwt
        token = AccountService.login(account, session=db.session(), ip_address=extract_remote_ip(request))
        return token, ""

    @classmethod
    def user_third_party(cls, code: str):
        """
        第三方钉钉登录
        返回: (token_pair, redirect_url, error)
        """
        userToken, err = cls.get_user_token(code)

        if err != "":
            return None, "", f"Failed to obtain token: {err}"
        response = requests.get(
            "https://api.dingtalk.com/v1.0/contact/users/me",
            headers={"x-acs-dingtalk-access-token": userToken},
        )
        # Check the response status code
        if response.status_code != 200:
            return None, "", f"Request failed, status code: {response.status_code}, msg: {response.text}"
        # Print the response content
        req = response.json()
        if "statusCode" in req and req["statusCode"] != 200:
            return None, "", f"Request failed,  msg: {req.message}"
        # 提取userid
        dingTalkToken, err = cls.get_access_token()
        unionIdResponse = requests.post(
            f"https://oapi.dingtalk.com/topapi/user/getbyunionid?access_token={dingTalkToken}",
            json={"unionid": req["unionId"]},
        )
        # Check the response status code
        if unionIdResponse.status_code != 200:
            return (
                None,
                "",
                f"unionIdResponse failed, status code: {unionIdResponse.status_code}, msg: {unionIdResponse.text}",
            )
        # Print the response content
        unionIdReq = unionIdResponse.json()
        if unionIdReq["errcode"] != 0:
            return None, "", f"Request failed,  msg: {unionIdReq['errmsg']}"

        token_pair, err = cls.auto_create_user(unionIdReq["result"]["userid"])
        if len(err) > 0:
            return None, "", "Request failed: " + err

        redirect_url = f"{dify_config.CONSOLE_WEB_URL}/explore/apps-center-extend"
        return token_pair, redirect_url, ""

    @classmethod
    def get_user_info(cls, code: str):
        """
        获取用户信息并登录
        返回: (token_pair, redirect_url, error)
        """
        host = "https://oapi.dingtalk.com/topapi/v2/user"
        token, err = cls.get_access_token()
        if err != "":
            return None, "", f"Failed to obtain token: {err}"
        response = requests.post(
            f"{host}/getuserinfo?access_token={token}",
            json={"code": code},
        )
        # Check the response status code
        if response.status_code != 200:
            return None, "", f"Request failed, status code: {response.status_code}"
        # Print the response content
        req = response.json()
        if req["errcode"] != 0:
            return None, "", "Request failed: " + req["errmsg"]
        token_pair, err = cls.auto_create_user(req["result"]["userid"])
        if len(err) != 0:
            return None, "", "Request failed: " + err

        redirect_url = f"{dify_config.CONSOLE_WEB_URL}/explore/apps-center-extend"
        return token_pair, redirect_url, ""
