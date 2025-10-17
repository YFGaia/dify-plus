import time
from functools import wraps

import jwt
from flask import request

from configs import dify_config


def repost_login_required(func):
    """
    If you decorate a view with this, it will ensure that the current user is logged in and authenticated via proxy
    forwarding before calling the actual view. (If not, it will call the :attr:`LoginManager.unauthorized` callback.)
    For example::

        @app.route('/post')
        @repost_login_required
        def post():
            pass
    """

    @wraps(func)
    def decorated_view(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if auth_header is None:
            auth_header = request.cookies.get("x-token")
        try:
            if auth_header is not None:
                auth_header = auth_header[7:] if "Bearer " in auth_header else auth_header
                decoded_token = jwt.decode(auth_header, dify_config.SECRET_KEY.encode(), algorithms=["HS256"])
                user_id = decoded_token.get("user_id")
                if user_id and time.time() < decoded_token.get("exp", 0):
                    kwargs["account"] = user_id
                    return func(*args, **kwargs)
        except jwt.ExpiredSignatureError:
            return {
                "code": 401,
                "status": "token has expired",
                "message": "account_token_has_expired",
            }
        except jwt.InvalidTokenError:
            return {
                "code": 401,
                "status": "token is invalid",
                "message": "account_token_is_invalid",
            }
        return {
            "code": 403,
            "status": "account_not_link_tenant",
            "message": "Account not link tenant.",
        }

    return decorated_view
