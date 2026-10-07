from datetime import UTC, datetime, timedelta

import jwt
from flask import Response, make_response, request
from flask_restx import Resource
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden

from configs import dify_config
from constants import COOKIE_NAME_LOGIN_CONFIG_TOKEN, HEADER_NAME_LOGIN_CONFIG_TOKEN
from controllers.common.schema import register_response_schema_models
from controllers.common.session import with_session
from controllers.console.flask_admission import console_account_admission
from extensions.ext_application_services import application_services
from fields.base import ResponseModel
from libs.helper import dump_response, extract_remote_ip
from machinery.context import RequestContext
from services.entities.feature_entities import (
    FeatureModel,
    LicenseModel,
    LimitationModel,
    SystemFeatureModel,
    VectorSpaceLimitationModel,
)
from services.login_config_service_extend import LoginConfigModelExtend, get_login_config_extend

from . import console_ns
from .wraps import cloud_utm_record


class TrialModelsResponse(ResponseModel):
    trial_models: list[str]


class AppDslVersionResponse(ResponseModel):
    app_dsl_version: str


class LoginConfigBootstrapResponse(ResponseModel):
    ok: bool
    token: str


class LoginConfigResponse(LoginConfigModelExtend, ResponseModel):
    """Console-only login configuration, serialized from the fork service result."""


register_response_schema_models(
    console_ns,
    AppDslVersionResponse,
    FeatureModel,
    LicenseModel,
    LoginConfigBootstrapResponse,
    LoginConfigResponse,
    LimitationModel,
    SystemFeatureModel,
    TrialModelsResponse,
    VectorSpaceLimitationModel,
)


def _issue_login_config_jwt(ip: str) -> str:
    return jwt.encode(
        {"ip": ip, "exp": datetime.now(UTC) + timedelta(hours=1), "type": "login_config"},
        dify_config.SECRET_KEY,
        algorithm="HS256",
    )


def _verify_login_config_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(
            token, dify_config.SECRET_KEY, algorithms=["HS256"], options={"require": ["exp", "ip", "type"]}
        )
    except jwt.PyJWTError:
        return False
    return payload["type"] == "login_config" and payload["ip"] == extract_remote_ip(request)


@console_ns.route("/login_config_bootstrap")
class LoginConfigBootstrapApi(Resource):
    @console_ns.response(200, "Success", console_ns.models[LoginConfigBootstrapResponse.__name__])
    def get(self) -> Response:
        token = _issue_login_config_jwt(extract_remote_ip(request))
        response = make_response(dump_response(LoginConfigBootstrapResponse, {"ok": True, "token": token}))
        response.set_cookie(
            COOKIE_NAME_LOGIN_CONFIG_TOKEN, token, max_age=3600, httponly=True, samesite="Lax", secure=request.is_secure
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@console_ns.route("/login_config")
class LoginConfigApi(Resource):
    @console_ns.response(200, "Success", console_ns.models[LoginConfigResponse.__name__])
    @console_ns.response(403, "Missing or invalid login_config token")
    @with_session(write=False)
    def get(self, session: Session) -> Response:
        token = request.cookies.get(COOKIE_NAME_LOGIN_CONFIG_TOKEN) or request.headers.get(
            HEADER_NAME_LOGIN_CONFIG_TOKEN
        )
        if not _verify_login_config_token(token):
            raise Forbidden("Missing or invalid login_config token; call /login_config_bootstrap first.")
        public_features = application_services().feature_queries.get_public_system_features()
        response = make_response(
            dump_response(LoginConfigResponse, get_login_config_extend(public_features, session=session))
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@console_ns.route("/features")
class FeatureApi(Resource):
    @console_ns.doc("get_tenant_features")
    @console_ns.doc(description="Get feature availability and limits for the current workspace")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[FeatureModel.__name__],
    )
    @console_account_admission()
    @cloud_utm_record
    def get(self, request_context: RequestContext):
        """Get current workspace features."""
        return dump_response(FeatureModel, application_services().feature_queries.get_features(request_context))


@console_ns.route("/features/vector-space")
class FeatureVectorSpaceApi(Resource):
    @console_ns.doc("get_tenant_feature_vector_space")
    @console_ns.doc(description="Get vector-space usage and limit for current tenant")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[VectorSpaceLimitationModel.__name__],
    )
    @console_account_admission()
    @cloud_utm_record
    def get(self, request_context: RequestContext):
        """Get vector-space usage and limit for current tenant"""
        return application_services().feature_queries.get_vector_space(request_context).model_dump()


@console_ns.route("/trial-models")
class TrialModelsApi(Resource):
    @console_ns.doc("get_trial_models")
    @console_ns.doc(description="Get hosted credit model provider configuration for the current workspace")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[TrialModelsResponse.__name__],
    )
    @console_account_admission()
    def get(self, request_context: RequestContext):
        """Get hosted credit provider configuration for the current workspace."""
        return dump_response(
            TrialModelsResponse,
            {"trial_models": application_services().feature_queries.get_trial_models(request_context)},
        )


@console_ns.route("/app-dsl-version")
class AppDslVersionApi(Resource):
    @console_ns.doc("get_app_dsl_version")
    @console_ns.doc(description="Get current app DSL version")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[AppDslVersionResponse.__name__],
    )
    def get(self):
        """Get current app DSL version for workflow clipboard compatibility."""
        return dump_response(
            AppDslVersionResponse,
            {"app_dsl_version": application_services().feature_queries.get_app_dsl_version()},
        )


@console_ns.route("/system-features")
class SystemFeatureApi(Resource):
    @console_ns.doc("get_system_features")
    @console_ns.doc(
        description="Get the non-sensitive bootstrap snapshot exposed before Console or Web authentication. "
        "This is not a general feature registry."
    )
    @console_ns.response(
        200,
        "Success",
        console_ns.models[SystemFeatureModel.__name__],
    )
    def get(self):
        """Get the non-sensitive bootstrap snapshot exposed before authentication.

        Authentication configuration must be available before the authentication flow can be selected.
        Authenticated license detail is served separately by SystemFeatureLicenseApi.
        """
        return dump_response(SystemFeatureModel, application_services().feature_queries.get_public_system_features())


@console_ns.route("/system-features/license")
class SystemFeatureLicenseApi(Resource):
    @console_ns.doc("get_system_license")
    @console_ns.doc(description="Get license status and usage detail")
    @console_ns.response(
        200,
        "Success",
        console_ns.models[LicenseModel.__name__],
    )
    @console_account_admission()
    def get(self, _request_context: RequestContext):
        """Get full license detail (status, expiry, workspace/seat usage).

        Authenticated counterpart to the license *status* exposed on the public
        system-features endpoint.
        """
        return application_services().feature_queries.get_license().model_dump()
