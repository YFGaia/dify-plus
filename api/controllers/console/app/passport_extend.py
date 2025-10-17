from datetime import UTC, datetime, timedelta

from flask import request
from flask_restx import Resource
from werkzeug.exceptions import NotFound, Unauthorized

from configs import dify_config
from controllers.console import api
from controllers.console.app.error_extend import WebSSOAuthRequiredError
from controllers.console.workspace.workspace import account_initialization_required, setup_required
from controllers.web.passport import generate_session_id
from extensions.ext_database import db
from libs.login import login_required
from libs.passport import PassportService
from models.model import App, EndUser, Site
from services.feature_service import FeatureService


class PassportResourceExtend(Resource):
    """Base resource for passport."""

    @setup_required
    @login_required
    @account_initialization_required
    def get(self):
        system_features = FeatureService.get_system_features()
        if system_features.sso_enforced_for_web:
            raise WebSSOAuthRequiredError()

        app_code = request.headers.get("X-App-Code")
        if app_code is None:
            raise Unauthorized("X-App-Code header is missing.")

        # 二开部分Begin - 校验Token
        auth_header = request.headers.get("Authorization-extend", "")
        if not auth_header:
            auth_token = request.args.get("_token")
            if not auth_token:
                raise WebSSOAuthRequiredError()
        else:
            if " " not in auth_header:
                raise Unauthorized("Invalid Authorization header format. Expected 'Bearer <api-key>' format.")
            auth_scheme, auth_token = auth_header.split(None, 1)
            auth_scheme = auth_scheme.lower()
            if auth_scheme != "bearer":
                raise Unauthorized("Invalid Authorization header format. Expected 'Bearer <api-key>' format.")

        decoded = PassportService().verify(auth_token)
        user_id = decoded.get("user_id")
        # 二开部分End - 校验Token

        # get site from db and check if it is normal
        site = db.session.query(Site).filter(Site.code == app_code, Site.status == "normal").first()
        if not site:
            print("site", site, flush=True)
            raise NotFound()
        # get app from db and check if it is normal and enable_site
        app_model = db.session.query(App).filter(App.id == site.app_id).first()
        if not app_model or app_model.status != "normal" or not app_model.enable_site:
            print("app_model", app_model, flush=True)
            print("app_model", app_model, flush=True)
            raise NotFound()

        endUser_ta = db.session.query(EndUser).filter(EndUser.id == user_id).first()
        if not endUser_ta:
            end_user = EndUser(
                id=user_id,
                tenant_id=app_model.tenant_id,
                app_id=app_model.id,
                type="browser",
                is_anonymous=True,
                session_id=generate_session_id(),
            )

            db.session.add(end_user)
            db.session.commit()
        exp_dt = datetime.now(UTC) + timedelta(minutes=dify_config.ACCESS_TOKEN_EXPIRE_MINUTES)
        exp = int(exp_dt.timestamp())
        payload = {
            "iss": site.app_id,
            "sub": "Web API Passport",
            "app_id": site.app_id,
            "app_code": app_code,
            "end_user_id": user_id,
            "exp": exp,
        }

        tk = PassportService().issue(payload)

        return {
            "access_token": tk,
        }


api.add_resource(PassportResourceExtend, "/passport-extend")
