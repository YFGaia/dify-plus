import json
import logging
import secrets
import time

import requests
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models
from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util.client import Client as UtilClient
from flask import request
from pypinyin import lazy_pinyin

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
    def create_client(cls) -> dingtalkoauth2_1_0Client:
        """
        使用 Token 初始化账号Client
        @return: Client
        @throws Exception
        """
        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        return dingtalkoauth2_1_0Client(config)

    @classmethod
    def get_user_token(cls, code: str) -> (str, str):
        # get token
        client = cls.create_client()
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend).filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK).first()
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
        if DINGTALK_ACCOUNT_TOKEN["time"] > time.time():
            return DINGTALK_ACCOUNT_TOKEN["token"], ""
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend).filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK).first()
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
        dingTalkToken, err = cls.get_access_token()
        responses = requests.post(
            f'https://oapi.dingtalk.com/topapi/v2/user/get?access_token={dingTalkToken}',
            json={"userid": userid},
        )
        # Check the response status code
        if responses.status_code != 200:
            return "", f"Request for user information failed, status code: {responses.status_code}"
        reqs = responses.json()
        if reqs["errcode"] != 0:
            return "", "Request for user information failed: " + userid + " " + json.dumps(reqs)
        # Check if the user exists
        username = reqs["result"]['name']
        email = f"{''.join(lazy_pinyin(username))}@{dify_config.EMAIL_DOMAIN}"
        if "email" in reqs["result"] and len(reqs["result"]["email"]):
            email = reqs["result"]["email"]
        account: Account = (
            db.session.query(Account).filter(Account.email == email).first()
        )
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
                    TenantService.switch_tenant(account, super_admin_tenant_id)
        # token jwt
        token = AccountService.login(account, ip_address=extract_remote_ip(request))
        return token, ""

    @classmethod
    def user_third_party(cls, code: str) -> (str, str):
        userToken, err = cls.get_user_token(code)

        if err != "":
            return "", f"Failed to obtain token: {err}"
        response = requests.get(
            "https://api.dingtalk.com/v1.0/contact/users/me",
            headers={"x-acs-dingtalk-access-token": userToken},
        )
        # Check the response status code
        if response.status_code != 200:
            return "", f"Request failed, status code: {response.status_code}, msg: {response.text}"
        # Print the response content
        req = response.json()
        if "statusCode" in req.keys() and req["statusCode"] != 200:
            return "", f"Request failed,  msg: {req.message}"
        # 提取userid
        dingTalkToken, err = cls.get_access_token()
        unionIdResponse = requests.post(
            f"https://oapi.dingtalk.com/topapi/user/getbyunionid?access_token={dingTalkToken}",
            json={"unionid": req["unionId"]}
        )
        # Check the response status code
        if unionIdResponse.status_code != 200:
            return "", f"unionIdResponse failed, status code: {unionIdResponse.status_code}, msg: {unionIdResponse.text}"
        # Print the response content
        unionIdReq = unionIdResponse.json()
        if unionIdReq["errcode"] != 0:
            return "", f"Request failed,  msg: {unionIdReq['errmsg']}"

        token, err = cls.auto_create_user(unionIdReq["result"]["userid"])
        if len(err) > 0:
            return "", "Request failed: " + err

        return f"{dify_config.CONSOLE_WEB_URL}/explore/apps-center-extend?console_token={token.access_token}&&refresh_token={token.refresh_token}", ""

    @classmethod
    def get_user_info(cls, code: str) -> (str, str):
        host = "https://oapi.dingtalk.com/topapi/v2/user"
        token, err = cls.get_access_token()
        if err != "":
            return "", f"Failed to obtain token: {err}"
        response = requests.post(
            f"{host}/getuserinfo?access_token={token}",
            json={"code": code},
        )
        # Check the response status code
        if response.status_code != 200:
            return "", f"Request failed, status code: {response.status_code}"
        # Print the response content
        req = response.json()
        if req["errcode"] != 0:
            return "", "Request failed: " + req["errmsg"]
        token, err = cls.auto_create_user(req["result"]["userid"])
        if len(err) != 0:
            return "", "Request failed: " + err

        return f"{dify_config.CONSOLE_WEB_URL}/explore/apps-center-extend?console_token={token.access_token}&&refresh_token={token.refresh_token}", ""
