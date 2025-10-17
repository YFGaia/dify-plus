import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime

import requests
from flask import Response, request

from configs import dify_config
from extensions.ext_database import db
from models.account import Account
from models.account_money_extend import AccountLayoverRecordExtend, AccountMoneyExtend
from models.ai_draw_extnd import ForwardingAddressExtend

# Create a shared dictionary
billing = {}
# Create a lock object
dict_lock = threading.Lock()


def thread_billing_write(key: str, billing_info: ForwardingAddressExtend):
    global billing
    with dict_lock:
        billing[key] = [
            json.dumps(
                {
                    "id": billing_info.id,
                    "path": billing_info.path,
                    "models": billing_info.models,
                    "status": billing_info.status,
                    "billing": billing_info.billing,
                    "description": billing_info.description,
                    "content_type": billing_info.content_type,
                    "forwarding_id": billing_info.forwarding_id,
                }
            ),
            int(time.time()),
        ]


def thread_billing_read(forwarding_id: str, path: str) -> ForwardingAddressExtend | None:
    global billing
    url_path = "/".join(path.split("/")[1:])
    key = "{}_{}".format(forwarding_id, url_path)
    info = billing.get(key)
    if info is not None and info[1] < int(time.time()) + 600:
        if info[0] is not None:
            address_dict_back = json.loads(info[0])
            return ForwardingAddressExtend(
                id=address_dict_back["id"],
                path=address_dict_back["path"],
                models=address_dict_back["models"],
                status=address_dict_back["status"],
                billing=address_dict_back["billing"],
                description=address_dict_back["description"],
                content_type=address_dict_back["content_type"],
                forwarding_id=address_dict_back["forwarding_id"],
            )
    billing_info: ForwardingAddressExtend = (
        db.session.query(ForwardingAddressExtend)
        .filter(ForwardingAddressExtend.forwarding_id == forwarding_id, ForwardingAddressExtend.path == url_path)
        .first()
    )
    if billing_info is not None:
        thread_billing_write(key, billing_info)
    else:
        billing[key] = [None, int(time.time())]
    return billing_info


class AiDrawBilling:
    @classmethod
    def calculate_user_billing_information(cls, account_id: str, forwarding: str, path: str, data: dict) -> (int, str):
        """
        Handling fee processing for forward transmission
        :param account_id: string
        :param forwarding: string
        :param path: string
        :param data: dict
        """
        account: Account = db.session.query(Account).filter(Account.id == account_id).first()
        if account is None:
            return 0, "user does not exist"
        info: ForwardingAddressExtend = thread_billing_read(forwarding, path)
        if info is None:
            return 0, "count not found"
        # differentiate request types
        funds, money = info.funds_settlement(data, info.decode_billing)
        # 计费
        account_money = db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == account.id).first()
        if account_money:
            if float(account_money.used_quota) + money > float(account_money.total_quota):
                return 500, "Insufficient balance"
            db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == account.id).update(
                {"used_quota": float(account_money.used_quota) + money}
            )
        else:
            account_money_add = AccountMoneyExtend(
                account_id=account.id,
                used_quota=money,
                total_quota=15,  # TODO 初始总额度这里到时候默认15要改
            )
            db.session.add(account_money_add)
        # 储存记录
        db.session.add(
            AccountLayoverRecordExtend(
                account_id=account_id, forwarding_id=forwarding, money=money, info=funds, created_at=datetime.now()
            )
        )
        db.session.commit()

        return money, ""

    @classmethod
    def ocr_translate(cls, image_base64, to_lang_code, from_code):
        # 获取凭据
        if not dify_config.YOUDAO_APP_KEY or not dify_config.YOUDAO_APP_SECRET:
            return "", "请在配置文件中设置有道翻译的APP_KEY和APP_SECRET"

        # 准备API请求参数
        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))

        # 计算input
        if len(image_base64) <= 20:
            input_str = image_base64
        else:
            input_str = image_base64[:10] + str(len(image_base64)) + image_base64[-10:]

        # 计算签名
        sign_str = dify_config.YOUDAO_APP_KEY + input_str + salt + curtime + dify_config.YOUDAO_APP_SECRET
        sign = hashlib.sha256(sign_str.encode('utf-8')).hexdigest()

        # 发送请求
        try:
            response = requests.post(
                'https://openapi.youdao.com/ocrtransapi',
                data={
                    'type': '1',  # Base64类型
                    'q': image_base64,
                    'from': from_code,
                    'to': to_lang_code,
                    'appKey': dify_config.YOUDAO_APP_KEY,
                    'salt': salt,
                    'sign': sign,
                    'signType': 'v3',
                    'curtime': curtime,
                    'render': '1',
                    'docType': 'json'
                },
                timeout=30
            )
            result = response.json()

            # 检查错误码
            if result.get('errorCode') == '0':
                return result.get('render_image', ''), ""
            return "", f"请求失败: {result.get('msg')}"

        except Exception as e:
            return "", f"翻译出错: {str(e)}"

    @classmethod
    def billing_forward(cls, forwarding, path_list, kwargs, auth_header, path):
        # Get request method
        method = request.method
        target_url = f"{forwarding.address}{'/'.join(path_list[1:])}"

        # Get request data
        try:
            data = request.get_data()
        except:
            data = ""
        try:
            cache_data = request.get_json()
        except:
            cache_data = {}
        # calculate user deduction information
        for key, value in request.args.items():
            cache_data[key] = value
        for key, value in request.form.items():
            cache_data[key] = value
        # Wait for an asynchronous task to complete and get the return value
        headers = {key: value for key, value in request.headers if key != "Host"}
        # Wait for an asynchronous task to complete and get the return value
        money, err = cls.calculate_user_billing_information(kwargs.get("account", ''), forwarding.id, path, cache_data)
        if len(err) > 0 and money == 500:
            return Response(err, status=500)
        for key, value in json.loads(forwarding.header):
            headers[key] = value
        # Set Cookie - 移除Bearer前缀
        token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else auth_header
        headers["cookie"] = f"x-token={token};"
        # Disable gzip compression
        headers["Accept-Encoding"] = "identity"
        # Forward the request according to the request method
        logging.warning("target_url: {}. json: {}".format(target_url, json.dumps(request.args)))
        logging.warning("headers: {}".format(json.dumps(headers)))
        try:
            if method == 'GET':
                resp = requests.get(target_url, headers=headers, params=request.args, allow_redirects=False)
            elif method == "POST":
                resp = requests.post(target_url, headers=headers, data=data, params=request.args)
            elif method == "PUT":
                resp = requests.put(target_url, headers=headers, data=data, params=request.args)
            elif method == "DELETE":
                resp = requests.delete(target_url, headers=headers, data=data, params=request.args)
            else:
                return Response("Method not allowed", status=405)
            
            logging.warning("Response status: {}, content: {}".format(resp.status_code, resp.text[:500]))
        except Exception as e:
            logging.exception("Request failed: {}".format(str(e)))
            return Response("Forward request failed: {}".format(str(e)), status=500)

        # Create response
        response = Response(resp.content, status=resp.status_code)
        for key, value in resp.headers.items():
            response.headers[key] = value
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, DELETE"
        response.headers["Access-Control-Max-Age"] = "3600"
        response.headers["Access-Control-Allow-Headers"] = "x-requested-with,Authorization,token, content-type"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["X-Accel-Redirect"] = ""
        try:
            # Compatible processing
            body = response.get_json()
            if body is not None and isinstance(body, dict):
                if "metadata" in body.keys():
                    if "usage" in body["metadata"].keys():
                        body["metadata"]["usage"]["total_price"] = money
                    else:
                        body["metadata"]["usage"] = {"total_price": money}
                else:
                    body["metadata"] = {"usage": {"total_price": money}}
                # json encode
                body = json.dumps(body)
                if body is not None and body != "null" and body != any:
                    response.data = body
        except:
            pass
        return response
