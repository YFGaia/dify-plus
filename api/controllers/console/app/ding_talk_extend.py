from flask import redirect, request
from flask_restx import Resource

from controllers.console.app.error_extend import DingTalkNotExist
from controllers.console.wraps import setup_required
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
        token, err = DingTalkService.get_user_info(code)
        if len(err) > 0:
            raise DingTalkNotExist(err)
        return redirect(token)


class DingTalkThirdParty(Resource):
    @setup_required
    def get(self):
        """
        DingTalk login
        """
        code = request.args.get("authCode", "")
        if not (0 < len(code) < 500):
            raise DingTalkNotExist
        token, err = DingTalkService.user_third_party(code)
        if len(err) > 0:
            raise DingTalkNotExist(err)
        return redirect(token)


api.add_resource(DingTalk, "/ding-talk/login")
api.add_resource(DingTalkThirdParty, "/ding-talk/third-party/login")
