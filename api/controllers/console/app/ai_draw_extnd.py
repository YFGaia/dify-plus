"""
转发相关接口
Created on 2024-03-21
"""

import concurrent.futures
import logging

from flask import Response, current_app, request
from flask_restful import Resource

from controllers.console import api
from libs.login_extend import repost_login_required
from services.ai_draw_extend import AiDrawForwarding
from services.billing_extend import AiDrawBilling

logging.basicConfig(level=logging.DEBUG)
# 创建一个线程池
executor = concurrent.futures.ThreadPoolExecutor()


class AiDrawTransit(Resource):
    def __init__(self, *args, **kwargs):
        # Destination address
        self.target_url = current_app.config.get("HOSTED_FETCH_APP_TEMPLATES_MODE")

    def get(self, path):
        pass

    def post(self, path):
        pass

    def put(self, path):
        pass

    def delete(self, path):
        pass

    def patch(self, path):
        pass

    def options(self, path):
        pass

    @repost_login_required
    def dispatch_request(self, *args, **kwargs):
        # Replace with the address of the target server
        print('1111')
        path = kwargs.get("path", "")
        path_list = path.split("/")
        auth_header = request.headers.get("Authorization")
        if auth_header is None:
            auth_header = "Bearer " + request.cookies.get("x-token")
        if len(path_list) < 1:
            return Response("router error", status=500)
        # obtains forwarding domain name
        logging.warning("obtains forwarding domain name: {}".format(path_list[0]))
        forwarding = AiDrawForwarding.get_forwarding(path_list[0])
        print(forwarding)
        logging.warning("forwarding: {}".format(forwarding.id))
        if forwarding is None:
            return Response("router is none", status=500)
        # 使用线程池来运行异步函数
        return AiDrawBilling.billing_forward(forwarding, path_list, kwargs, auth_header, path)


# class YouDaoTranslationPictures(Resource):
#     """有道翻译图片接口"""
#
#     @setup_required
#     @login_required
#     def post(self):
#         """
#         翻译图片接口
#         ---
#         请求参数:
#             - images: list[str] base64编码的图片列表
#             - language: str 目标语言代码
#         返回:
#             - code: int 状态码
#             - message: str 提示信息
#             - data: list[str] 翻译后的base64图片列表
#         """
#         parser = reqparse.RequestParser()
#         parser.add_argument("language", type=str, required=True, location="json")
#         parser.add_argument("image", type=str, required=True, location="json")
#         parser.add_argument("from_code", type=str, required=True, location="json")
#         args = parser.parse_args()
#
#         if not args.image or not args.language:
#             response_data = {"code": 400, "message": '参数错误:images和language不能为空', "data": None}
#             response = make_response(response_data)
#             self._add_cors_headers(response)
#             return response
#
#         # 翻译图片
#         forwarding = AiDrawForwarding.get_forwarding("youdao_ocr_translate")
#         if forwarding is not None:
#             AiDrawBilling.calculate_user_billing_information(current_user.id, forwarding.id, "/translate", args)
#         img_url, err = AiDrawBilling.ocr_translate(
#             image_base64=args.image,
#             from_code=args.from_code,
#             to_lang_code=args.language,
#         )
#         if err != "":
#             response_data = {"code": 500, "message": err, "data": None}
#             response = make_response(response_data)
#             self._add_cors_headers(response)
#             return response
#         else:
#             # Extend start: 绘图 翻译图片有道的base64改储存到本地
#             try:
#                 # 解码 base64 图片数据
#                 extension = 'png'
#                 mime_type = 'image/png'
#
#                 # 确保 base64 字符串格式正确
#                 base64_data = img_url
#                 # 如果 img_url 已经包含 data URL 前缀，提取纯 base64 部分
#                 if base64_data.startswith('data:image/'):
#                     base64_data = base64_data.split(',', 1)[1]
#
#                 # 添加必要的 padding
#                 missing_padding = len(base64_data) % 4
#                 if missing_padding:
#                     base64_data += '=' * (4 - missing_padding)
#
#                 # 解码 base64 数据
#                 image_content = base64.b64decode(base64_data)
#
#                 # 生成文件名
#                 filename = f"translated_image_{uuid.uuid4().hex[:8]}.{extension}"
#
#                 # 使用 FileService 保存文件
#                 upload_file = FileService.upload_file(
#                     filename=filename,
#                     content=image_content,
#                     mimetype=mime_type,
#                     user=current_user
#                 )
#
#                 # 生成可访问的 URL
#                 base_url = dify_config.FILES_URL
#                 image_preview_url = f"{base_url}/files/{upload_file.id}/image-preview"
#                 signed_url = UrlSigner.get_signed_url(
#                     url=image_preview_url,
#                     sign_key=upload_file.id,
#                     prefix="image-preview"
#                 )
#
#                 response_data = {
#                     'code': 200,
#                     'message': '翻译成功',
#                     'data': {
#                         'image_url': signed_url,
#                         'file_id': upload_file.id
#                     }
#                 }
#                 response = make_response(response_data)
#                 self._add_cors_headers(response)
#                 return response
#
#             except Exception as e:
#                 logging.error(f"保存翻译图片失败: {str(e)}")
#                 response_data = {"code": 500, "message": f'保存翻译图片失败: {str(e)}', "data": None}
#                 response = make_response(response_data)
#                 self._add_cors_headers(response)
#                 return response
#             # Extend stop: 绘图 翻译图片有道的base64改储存到本地
#
    def _add_cors_headers(self, response):
        """添加CORS头部"""
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, DELETE"
        response.headers["Access-Control-Max-Age"] = "3600"
        response.headers["Access-Control-Allow-Headers"] = "x-requested-with,Authorization,token, content-type"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["X-Accel-Redirect"] = ""


api.add_resource(AiDrawTransit, "/extend/<path:path>")
# api.add_resource(YouDaoTranslationPictures, "/youdao/translation/pictures")
