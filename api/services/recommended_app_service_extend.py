import logging

from flask_login import current_user
from sqlalchemy import select
from sqlalchemy.sql import Select
from werkzeug.exceptions import NotFound

from extensions.ext_database import db
from models.model import (
    App,
    Conversation,
    InstalledApp,
    RecommendedApp,
    Tag,
    TagBinding,
)
from models.model_extend import AppStatisticsExtend  # Extend: App Center
from services.account_service_extend import TenantExtendService

logger = logging.getLogger(__name__)


class RecommendedAppService:
    @classmethod
    def installed_app_list(cls, tenant_id: str) -> dict:
        # -------------- start: add category to categories ---------------
        apps = (
            db.session.query(App)
            .join(AppStatisticsExtend, App.id == AppStatisticsExtend.app_id)
            .filter(App.tenant_id == tenant_id)
            .order_by(AppStatisticsExtend.number.desc())
            .all()
        )
        categories = set()
        recommended_apps_result = []

        for app in apps:
            classList = app.tags
            description = app.description
            config = app.app_model_config
            # Extend: start Handle apps without tags
            if len(classList) == 0:
                # Create a simple object with name attribute for "未分类" category
                classList.append(type("Tag", (), {"name": "未分类"})())
            # Extend: stop Handle apps without tags
            if (
                len(description) == 0
                and config is not None
                and config.pre_prompt is not None
                and len(config.pre_prompt) > 0
            ):
                description = config.pre_prompt
            for i in classList:
                category = i.name
                if i.name != "未分类":
                    categories.add(i.name)
                installed_app: InstalledApp = (
                    db.session.query(InstalledApp).filter(InstalledApp.app_id == app.id).first()
                )
                recommended_apps_result.append(
                    {
                        "id": installed_app.id,
                        "app": {
                            "id": installed_app.id,
                            "name": app.name,
                            "mode": app.mode,
                            "icon": app.icon,
                            "icon_type": app.icon_type,
                            "icon_background": app.icon_background,
                        },
                        "app_id": installed_app.app_id,
                        "installed_id": installed_app.id,
                        "description": description,
                        "copyright": "",
                        "privacy_policy": "",
                        "custom_disclaimer": "",
                        "category": category,
                        "position": 0,
                        "is_listed": True,
                    }
                )
        categories = sorted(categories, reverse=True)
        categories.insert(len(categories), "未分类")
        # -------------- stop: add category to categories ---------------
        return {"recommended_apps": recommended_apps_result, "categories": categories}  # add category to categories

    @classmethod
    def delete_sync_recommended_app(cls, app: str):
        # 分类数据随 recommended_apps.categories 一并删除（DD3：fork 分类表已退役）
        db.session.query(RecommendedApp).filter(RecommendedApp.app_id == app).delete()
        db.session.commit()

    @classmethod
    def sync_recommended_app(cls, app: str) -> str:
        """
        Sync an app into the explore recommended list (fork "sync to app center" feature).

        Categories are written to the upstream-native ``recommended_apps.categories``
        JSON column (DD3, openspec change p3-merge-upstream-1-15-0); the former fork
        tables ``recommended_category_extend`` / ``recommended_apps_category_join_extend``
        are retired and must not be written anymore. Categories are derived from the
        app's workspace tags at sync time.

        :param app: App ID (non-app tag targets fall through the except and return "")
        :return: RecommendedApp ID, or "" when unauthorized or on failure
        """
        # The role of the current user in the ta table must be admin or owner
        tenant_extend_service = TenantExtendService
        super_admin_id = tenant_extend_service.get_super_admin_id().id
        if super_admin_id != current_user.id:
            return ""
        try:
            # query application information
            appInfo: App = db.session.query(App).filter(App.id == app).first()
            appInfo.is_public = True
            db.session.commit()
            recommendedApp = db.session.query(RecommendedApp).filter(RecommendedApp.app_id == app).first()
            if recommendedApp is None:
                language_prefix = "zh-Hans"
                if current_user and current_user.interface_language:
                    language_prefix = current_user.interface_language
                # unable to find creation
                recommendedApp = RecommendedApp(
                    app_id=app,
                    position=0,
                    copyright="",
                    is_listed=True,
                    category="tag",
                    install_count=0,
                    privacy_policy="",
                    language=language_prefix,
                    custom_disclaimer="",
                    description=appInfo.description,
                )
                # insert statement
                db.session.add(recommendedApp)
                db.session.commit()
            # derive categories from the app's current tags and overwrite the JSON column
            bindings = db.session.query(TagBinding).filter(TagBinding.target_id == appInfo.id).all()
            tag_ids = [binding.tag_id for binding in bindings]
            categories: list[str] = []
            if tag_ids:
                tags = db.session.query(Tag).filter(Tag.id.in_(tag_ids)).all()
                categories = sorted({tag.name.strip() for tag in tags if tag.name and tag.name.strip()})
            recommendedApp.categories = categories
            db.session.commit()
            return recommendedApp.id
        except Exception:
            # 保持历史行为：同步失败（含非 App 的 tag target）静默返回空串，不打断 tag 解绑等主流程
            logger.exception("sync_recommended_app failed, app_id=%s", app)
            db.session.rollback()
            return ""

    # Extend: start messages context handling
    @classmethod
    def message_context_app(cls, *, tenant_id: str, conversation_id: str) -> App:
        """Resolve the trusted app owner without accessing any context markers."""
        app = (
            db.session.query(App)
            .join(Conversation, Conversation.app_id == App.id)
            .filter(
                App.tenant_id == tenant_id,
                Conversation.id == conversation_id,
                Conversation.is_deleted.is_(False),
            )
            .first()
        )
        if app is None:
            raise NotFound("Conversation not found")
        return app

    @staticmethod
    def _context_conversations(*, tenant_id: str, app_id: str, conversation_id: str) -> Select[tuple[str]]:
        """Keep the entire owner chain on marker reads and writes after authorization."""
        return (
            select(Conversation.id)
            .join(App, App.id == Conversation.app_id)
            .where(
                App.tenant_id == tenant_id,
                App.id == app_id,
                Conversation.id == conversation_id,
                Conversation.is_deleted.is_(False),
            )
        )

    @classmethod
    def message_context(cls, *, tenant_id: str, app_id: str, conversation_id: str) -> list[str]:
        from models.model_extend import MessageContextExtend

        conversations = cls._context_conversations(tenant_id=tenant_id, app_id=app_id, conversation_id=conversation_id)
        message_context = (
            db.session.query(MessageContextExtend)
            .filter(MessageContextExtend.conversation_id.in_(conversations))
            .order_by(MessageContextExtend.created_at.desc())
            .all()
        )
        return [context.message_id for context in message_context]

    @classmethod
    def delete_message_context(cls, *, tenant_id: str, app_id: str, conversation_id: str, message_id: str) -> str:
        from models.model_extend import MessageContextExtend

        conversations = cls._context_conversations(tenant_id=tenant_id, app_id=app_id, conversation_id=conversation_id)
        db.session.query(MessageContextExtend).filter(
            MessageContextExtend.conversation_id.in_(conversations),
            MessageContextExtend.message_id == message_id,
        ).delete(synchronize_session=False)
        db.session.commit()
        return "ok"

    # Extend: stop messages context handling
