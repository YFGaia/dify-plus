from flask import redirect, request
from flask_restx import Resource

from controllers.console.app.error_extend import DingTalkNotExist
from controllers.console.wraps import setup_required
from libs.token import (
    set_access_token_to_cookie,
    set_csrf_token_to_cookie,
    set_refresh_token_to_cookie,
)
from services.ding_talk_extend import DingTalkService

from .. import api


class DingTalk(Resource):
    @setup_required
    def get(self):
        """
        DingTalk login
        """
        code = request.args.get("code", "")
        if not (0 < len(code) < 500):
            raise DingTalkNotExist
        token_pair, redirect_url, err = DingTalkService.get_user_info(code)
        if len(err) > 0:
            raise DingTalkNotExist(err)
        if token_pair is None:
            raise DingTalkNotExist("Failed to get token pair")
        
        response = redirect(redirect_url)
        set_access_token_to_cookie(request, response, token_pair.access_token)
        set_refresh_token_to_cookie(request, response, token_pair.refresh_token)
        set_csrf_token_to_cookie(request, response, token_pair.csrf_token)
        return response


class DingTalkThirdParty(Resource):
    @setup_required
    def get(self):
        """
        DingTalk login
        """
        code = request.args.get("authCode", "")
        if not (0 < len(code) < 500):
            raise DingTalkNotExist
        token_pair, redirect_url, err = DingTalkService.user_third_party(code)
        if len(err) > 0:
            raise DingTalkNotExist(err)
        if token_pair is None:
            raise DingTalkNotExist("Failed to get token pair")
        
        response = redirect(redirect_url)
        set_access_token_to_cookie(request, response, token_pair.access_token)
        set_refresh_token_to_cookie(request, response, token_pair.refresh_token)
        set_csrf_token_to_cookie(request, response, token_pair.csrf_token)
        return response


api.add_resource(DingTalk, "/ding-talk/login")
api.add_resource(DingTalkThirdParty, "/ding-talk/third-party/login")
