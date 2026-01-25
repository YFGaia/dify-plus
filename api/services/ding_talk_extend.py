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
        path = re.sub(r'\[(\d+)\]', r'.[\1]', path)
        parts = path.split('.')
        current = dictionary
        
        for part in parts:
            if not part:
                continue
                
            # 处理数组索引
            array_match = re.match(r'\[(\d+)\]', part)
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
        try:
            # 解析config字段
            if not integration.config:
                return ""
                
            config_data = json.loads(integration.config)
            email_api_config = config_data.get("email_api", {})
            
            # 检查是否启用
            if not email_api_config.get("enabled", False):
                return ""
                
            # 获取配置参数
            api_url = email_api_config.get("url", "")
            method = email_api_config.get("method", "GET").upper()
            param_field = email_api_config.get("request_param_field", "userId")
            email_field = email_api_config.get("response_email_field", "data[0].userName")
            body_type = email_api_config.get("body_type", "raw")
            headers = email_api_config.get("headers", {})
            authorization = email_api_config.get("authorization", {})
            body_data = email_api_config.get("body_data", {})
            
            if not api_url:
                logger.warning("Third-party email API URL is not configured")
                return ""
            
            # 准备请求头
            request_headers = dict(headers) if headers else {}
            
            # 处理Authorization
            auth = None
            auth_type = authorization.get("type", "none")
            if auth_type == "bearer":
                token = authorization.get("token", "")
                if token:
                    request_headers["Authorization"] = f"Bearer {token}"
            elif auth_type == "basic":
                username = authorization.get("username", "")
                password = authorization.get("password", "")
                if username and password:
                    from requests.auth import HTTPBasicAuth
                    auth = HTTPBasicAuth(username, password)
            
            # 构建请求数据
            request_data = {}
            
            # 处理Body数据（仅POST/PUT/DELETE）
            if method in ["POST", "PUT", "DELETE"]:
                if body_type == "form-data":
                    # form-data: 合并body_data中的form_data
                    form_data_items = body_data.get("form_data", [])
                    for item in form_data_items:
                        if isinstance(item, dict) and "key" in item and "value" in item:
                            key = item.get("key", "").strip()
                            value = item.get("value", "").strip()
                            if key:
                                request_data[key] = value
                    # 确保主请求字段的值始终是userid（覆盖body_data中的值）
                    request_data[param_field] = userid
                    # form-data使用data参数
                    response = requests.request(
                        method, api_url, data=request_data, 
                        headers=request_headers, auth=auth, timeout=10
                    )
                elif body_type == "x-www-form-urlencoded":
                    # x-www-form-urlencoded: 合并body_data中的urlencoded
                    urlencoded_items = body_data.get("urlencoded", [])
                    for item in urlencoded_items:
                        if isinstance(item, dict) and "key" in item and "value" in item:
                            key = item.get("key", "").strip()
                            value = item.get("value", "").strip()
                            if key:
                                request_data[key] = value
                    # 确保主请求字段的值始终是userid（覆盖body_data中的值）
                    request_data[param_field] = userid
                    # 确保Content-Type正确
                    if "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
                    response = requests.request(
                        method, api_url, data=request_data,
                        headers=request_headers, auth=auth, timeout=10
                    )
                else:  # raw (JSON)
                    # raw: 合并body_data中的raw JSON
                    raw_json = body_data.get("raw", "")
                    if raw_json:
                        try:
                            raw_data = json.loads(raw_json)
                            if isinstance(raw_data, dict):
                                request_data.update(raw_data)
                        except json.JSONDecodeError:
                            logger.warning("Failed to parse raw JSON body: %s", raw_json)
                    # 确保主请求字段的值始终是userid（覆盖raw JSON中的值）
                    request_data[param_field] = userid
                    # 确保Content-Type正确
                    if "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json"
                    response = requests.request(
                        method, api_url, json=request_data,
                        headers=request_headers, auth=auth, timeout=10
                    )
            else:  # GET请求
                # GET请求：所有数据作为URL参数
                response = requests.get(
                    api_url, params=request_data,
                    headers=request_headers, auth=auth, timeout=10
                )
            
            # 检查响应
            if response.status_code != 200:
                logger.error(f"Third-party email API returned status code: {response.status_code}")
                return ""
            
            # 解析响应
            response_data = response.json()
            email = cls.extract_data(response_data, email_field)
            
            if email and isinstance(email, str) and "@" in email:
                logger.info("Successfully retrieved email from third-party API for userid: %s", userid)
                return email
            else:
                logger.warning("Failed to extract valid email from response using path: %s", email_field)
                return ""
                
        except json.JSONDecodeError as e:
            logger.error("Failed to parse email API config: %s", e)
            return ""
        except requests.exceptions.RequestException as e:
            logger.error("Failed to call third-party email API: %s", e)
            return ""
        except Exception as e:
            logger.error("Unexpected error in get_email_from_third_party_api: %s", e)
            return ""

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
        # 获取集成配置
        integration: SystemIntegrationExtend = (
            db.session.query(SystemIntegrationExtend).filter(
                SystemIntegrationExtend.status == True,
                SystemIntegrationExtend.classify == SystemIntegrationClassify.SYSTEM_INTEGRATION_DINGTALK).first()
        )
        
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
        
        # 优先尝试从第三方API获取邮箱
        email = ""
        if integration:
            email = cls.get_email_from_third_party_api(userid, integration)
        
        # 降级处理：使用钉钉返回的邮箱
        if not email and "email" in reqs["result"] and len(reqs["result"]["email"]):
            email = reqs["result"]["email"]
        
        # 最终降级：使用拼音生成邮箱
        if not email:
            email = f"{''.join(lazy_pinyin(username))}@{dify_config.EMAIL_DOMAIN}"
            logger.info("Using pinyin-generated email for user %s: %s", userid, email)
        
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
        if "statusCode" in req.keys() and req["statusCode"] != 200:
            return None, "", f"Request failed,  msg: {req.message}"
        # 提取userid
        dingTalkToken, err = cls.get_access_token()
        unionIdResponse = requests.post(
            f"https://oapi.dingtalk.com/topapi/user/getbyunionid?access_token={dingTalkToken}",
            json={"unionid": req["unionId"]}
        )
        # Check the response status code
        if unionIdResponse.status_code != 200:
            return None, "", f"unionIdResponse failed, status code: {unionIdResponse.status_code}, msg: {unionIdResponse.text}"
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
