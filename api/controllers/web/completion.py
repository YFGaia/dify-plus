import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from werkzeug.exceptions import BadRequest, InternalServerError, NotFound

import services
from controllers.common.fields import GeneratedAppResponse, SimpleResultResponse
from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.console.app.wraps import with_session
from controllers.web import web_ns
from controllers.web.error import (
    AgentNotPublishedError,
    AppUnavailableError,
    CompletionRequestError,
    ConversationCompletedError,
    NotChatAppError,
    NotCompletionAppError,
    ProviderModelCurrentlyNotSupportError,
    ProviderNotInitializeError,
    ProviderQuotaExceededError,
)
from controllers.web.error import InvokeRateLimitError as InvokeRateLimitHttpError
from controllers.web.wraps import WebApiResource
from core.app.apps.agent_app.errors import AgentAppNotPublishedError
from core.app.entities.app_invoke_entities import InvokeFrom
from core.errors.error import (
    ModelCurrentlyNotSupportError,
    ProviderTokenNotInitError,
    QuotaExceededError,
)
from graphon.model_runtime.errors.invoke import InvokeError
from libs import helper
from libs.helper import uuid_value
from models.model import App, AppMode, EndUser
from services.app_generate_service import AppGenerateService
from services.app_task_service import AppTaskService
from services.conversation_service import ConversationService
from services.errors.llm import InvokeRateLimitError

logger = logging.getLogger(__name__)


# extend: 您必须登录才能访问您的帐户扩展功能
from flask import request

from controllers.web.error_extend import (
    AccountNoMoneyErrorExtend,
    WebAuthRequiredErrorExtend,
)
from extensions.ext_database import db
from models.account_money_extend import AccountMoneyExtend
from services.app_generate_service_extend import AppGenerateServiceExtend
from services.webapp_auth_service_extend import WebAppAuthExtendService
from services.webapp_console_identity_extend import get_console_account_extend


def is_end_login(end_user):
    """extend: 从 WebApp 当前请求中解析 Console 用户，并在首次识别时绑定 external_user_id。"""
    user_info = None
    try:
        user_info = get_console_account_extend(request, session=db.session())

        # 绑定 end_user 与 Console 用户
        if user_info is not None:
            if end_user.external_user_id is None:
                end_user.external_user_id = user_info.id
                db.session.commit()  # 提交绑定关系
    except Exception:
        logging.exception("load_logged_in_account error")
        pass
    # no login
    return user_info


# 额度限制
def is_money_limit(end_user) -> bool:
    """extend: 依据 end_user 关联账户额度判断是否超限，异常时按安全默认值拦截。"""
    try:
        # TODO 需要写入缓存，读缓存
        account_money = (
            db.session.query(AccountMoneyExtend).filter(AccountMoneyExtend.account_id == end_user.id).first()
        )
        if not account_money:
            return False

        if account_money.used_quota >= account_money.total_quota:
            return True
        return False
    except:
        return True


# extend: 您必须登录才能访问您的帐户扩展功能


def _resolve_agent_app_streaming(*, app_mode: AppMode, response_mode: str | None) -> bool:
    """Agent App runtime is SSE-only until backend blocking runs are supported."""
    if app_mode != AppMode.AGENT:
        return response_mode == "streaming"
    if response_mode == "blocking":
        raise BadRequest("Agent App only supports streaming response mode.")
    return True


class CompletionMessagePayload(BaseModel):
    inputs: dict[str, Any] = Field(
        description="Input variables for the completion",
    )
    query: str = Field(default="", description="Query text for completion")
    files: list[dict[str, Any]] | None = Field(
        default=None,
        description="Files to be processed",
    )
    response_mode: Literal["blocking", "streaming"] | None = Field(
        default=None, description="Response mode: blocking or streaming"
    )
    retriever_from: str = Field(default="web_app", description="Source of retriever")


class ChatMessagePayload(BaseModel):
    inputs: dict[str, Any] = Field(
        description="Input variables for the chat",
    )
    query: str = Field(description="User query/message")
    files: list[dict[str, Any]] | None = Field(
        default=None,
        description="Files to be processed",
    )
    response_mode: Literal["blocking", "streaming"] | None = Field(
        default=None, description="Response mode: blocking or streaming"
    )
    conversation_id: str | None = Field(default=None, description="Conversation ID")
    parent_message_id: str | None = Field(default=None, description="Parent message ID")
    retriever_from: str = Field(default="web_app", description="Source of retriever")

    @field_validator("conversation_id", "parent_message_id")
    @classmethod
    def validate_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return uuid_value(value)


register_schema_models(web_ns, CompletionMessagePayload, ChatMessagePayload)
register_response_schema_models(web_ns, GeneratedAppResponse, SimpleResultResponse)


# define completion api for user
@web_ns.route("/completion-messages")
class CompletionApi(WebApiResource):
    @web_ns.doc("Create Completion Message")
    @web_ns.doc(description="Create a completion message for text generation applications.")
    @web_ns.expect(web_ns.models[CompletionMessagePayload.__name__])
    @web_ns.doc(
        responses={
            200: "Success",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "App Not Found",
            500: "Internal Server Error",
        }
    )
    @web_ns.response(200, "Success", web_ns.models[GeneratedAppResponse.__name__])
    @with_session
    def post(self, session: Session, app_model: App, end_user: EndUser):
        if app_model.mode != AppMode.COMPLETION:
            raise NotCompletionAppError()

        # ----------------- start You must log in to access your account extend ---------------
        # no login（per-app 认证开关关闭时允许匿名访问；已登录用户短路跳过开关查询）
        if is_end_login(end_user) is None and WebAppAuthExtendService.is_webapp_auth_enabled(app_model.id):
            raise WebAuthRequiredErrorExtend()
        # ----------------- stop You must log in to access your account extend ---------------

        # ----------------- 二开部分Begin - 余额判断-----------------
        if is_money_limit(end_user):
            raise AccountNoMoneyErrorExtend()
        # ----------------- 二开部分End - 余额判断-----------------

        payload = CompletionMessagePayload.model_validate(web_ns.payload or {})
        args = payload.model_dump(exclude_none=True)

        streaming = payload.response_mode == "streaming"
        args["auto_generate_name"] = False

        # extend 获取 Console 用户 ID，直接作为 from_account_id 传递
        user_info = is_end_login(end_user)
        if user_info:
            args["account_id"] = user_info.id

        try:
            AppGenerateServiceExtend.calculate_cumulative_usage(
                app_model=app_model,
                args=args,
            )  # Extend: App Center -
            # Recommended list sorted by usage frequency
            response = AppGenerateService.generate(
                session=session,
                app_model=app_model,
                user=end_user,
                args=args,
                invoke_from=InvokeFrom.WEB_APP,
                streaming=streaming,
            )

            # response-contract:ignore compact_generate_response
            return helper.compact_generate_response(response)
        except services.errors.conversation.ConversationNotExistsError:
            raise NotFound("Conversation Not Exists.")
        except services.errors.conversation.ConversationCompletedError:
            raise ConversationCompletedError()
        except services.errors.app_model_config.AppModelConfigBrokenError:
            logger.exception("App model config broken.")
            raise AppUnavailableError()
        except AgentAppNotPublishedError:
            raise AgentNotPublishedError()
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        except QuotaExceededError:
            raise ProviderQuotaExceededError()
        except ModelCurrentlyNotSupportError:
            raise ProviderModelCurrentlyNotSupportError()
        except InvokeError as e:
            raise CompletionRequestError(e.description)
        except ValueError as e:
            raise e
        except Exception as e:
            logger.exception("internal server error.")
            raise InternalServerError()


@web_ns.route("/completion-messages/<string:task_id>/stop")
class CompletionStopApi(WebApiResource):
    @web_ns.doc("Stop Completion Message")
    @web_ns.doc(description="Stop a running completion message task.")
    @web_ns.doc(params={"task_id": {"description": "Task ID to stop", "type": "string", "required": True}})
    @web_ns.doc(
        responses={
            200: "Success",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Task Not Found",
            500: "Internal Server Error",
        }
    )
    @web_ns.response(200, "Success", web_ns.models[SimpleResultResponse.__name__])
    def post(self, app_model: App, end_user: EndUser, task_id: str):
        if app_model.mode != AppMode.COMPLETION:
            raise NotCompletionAppError()

        AppTaskService.stop_task(
            task_id=task_id,
            invoke_from=InvokeFrom.WEB_APP,
            user_id=end_user.id,
            app_mode=AppMode.value_of(app_model.mode),
        )

        return SimpleResultResponse(result="success").model_dump(mode="json"), 200


@web_ns.route("/chat-messages")
class ChatApi(WebApiResource):
    @web_ns.doc("Create Chat Message")
    @web_ns.doc(description="Create a chat message for conversational applications.")
    @web_ns.expect(web_ns.models[ChatMessagePayload.__name__])
    @web_ns.doc(
        responses={
            200: "Success",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "App Not Found",
            500: "Internal Server Error",
        }
    )
    @web_ns.response(200, "Success", web_ns.models[GeneratedAppResponse.__name__])
    @with_session
    def post(self, session: Session, app_model: App, end_user: EndUser):
        # ----------------- start You must log in to access your account extend ---------------
        # no login（per-app 认证开关关闭时允许匿名访问；已登录用户短路跳过开关查询）
        if is_end_login(end_user) is None and WebAppAuthExtendService.is_webapp_auth_enabled(app_model.id):
            raise WebAuthRequiredErrorExtend()
        # ----------------- stop You must log in to access your account extend ---------------

        # ----------------- 二开部分Begin - 余额判断-----------------
        if is_money_limit(end_user):
            raise AccountNoMoneyErrorExtend()
        # ----------------- 二开部分End - 余额判断-----------------

        app_mode = AppMode.value_of(app_model.mode)
        if app_mode not in {AppMode.CHAT, AppMode.AGENT_CHAT, AppMode.ADVANCED_CHAT, AppMode.AGENT}:
            raise NotChatAppError()

        payload = ChatMessagePayload.model_validate(web_ns.payload or {})
        args = payload.model_dump(exclude_none=True)

        streaming = _resolve_agent_app_streaming(app_mode=app_mode, response_mode=payload.response_mode)
        args["auto_generate_name"] = False

        # 获取 Console 用户 ID，直接作为 from_account_id 传递
        user_info = is_end_login(end_user)
        if user_info:
            args["account_id"] = user_info.id

        try:
            AppGenerateServiceExtend.calculate_cumulative_usage(
                app_model=app_model,
                args=args,
            )  # Extend: App Center - Recommended list sorted by usage frequency
            # Eagerly validate conversation to avoid hanging on invalid conversation_id
            if payload.conversation_id:
                ConversationService.get_conversation(
                    app_model=app_model,
                    conversation_id=payload.conversation_id,
                    user=end_user,
                    session=session,
                )

            response = AppGenerateService.generate(
                session=session,
                app_model=app_model,
                user=end_user,
                args=args,
                invoke_from=InvokeFrom.WEB_APP,
                streaming=streaming,
            )

            # response-contract:ignore compact_generate_response
            return helper.compact_generate_response(response)
        except services.errors.conversation.ConversationNotExistsError:
            raise NotFound("Conversation Not Exists.")
        except services.errors.conversation.ConversationCompletedError:
            raise ConversationCompletedError()
        except services.errors.app_model_config.AppModelConfigBrokenError:
            logger.exception("App model config broken.")
            raise AppUnavailableError()
        except AgentAppNotPublishedError:
            raise AgentNotPublishedError()
        except ProviderTokenNotInitError as ex:
            raise ProviderNotInitializeError(ex.description)
        except QuotaExceededError:
            raise ProviderQuotaExceededError()
        except ModelCurrentlyNotSupportError:
            raise ProviderModelCurrentlyNotSupportError()
        except InvokeRateLimitError as ex:
            raise InvokeRateLimitHttpError(ex.description)
        except InvokeError as e:
            raise CompletionRequestError(e.description)
        except ValueError as e:
            raise e
        except Exception as e:
            logger.exception("internal server error.")
            raise InternalServerError()


@web_ns.route("/chat-messages/<string:task_id>/stop")
class ChatStopApi(WebApiResource):
    @web_ns.doc("Stop Chat Message")
    @web_ns.doc(description="Stop a running chat message task.")
    @web_ns.doc(params={"task_id": {"description": "Task ID to stop", "type": "string", "required": True}})
    @web_ns.doc(
        responses={
            200: "Success",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Task Not Found",
            500: "Internal Server Error",
        }
    )
    @web_ns.response(200, "Success", web_ns.models[SimpleResultResponse.__name__])
    def post(self, app_model: App, end_user: EndUser, task_id: str):
        app_mode = AppMode.value_of(app_model.mode)
        if app_mode not in {AppMode.CHAT, AppMode.AGENT_CHAT, AppMode.ADVANCED_CHAT, AppMode.AGENT}:
            raise NotChatAppError()

        AppTaskService.stop_task(
            task_id=task_id,
            invoke_from=InvokeFrom.WEB_APP,
            user_id=end_user.id,
            app_mode=app_mode,
        )

        return SimpleResultResponse(result="success").model_dump(mode="json"), 200
