from typing import Any, TypedDict

from sqlalchemy import select

from constants.languages import languages
from extensions.ext_database import db

# extend add category to categories
from models.model import App, RecommendedApp
from models.model_extend import RecommendedAppsCategoryJoinExtend, RecommendedCategoryExtend
from services.app_dsl_service import AppDslService
from services.recommend_app.recommend_app_base import RecommendAppRetrievalBase
from services.recommend_app.recommend_app_type import RecommendAppType


class RecommendedAppItemDict(TypedDict):
    id: str
    app: App | None
    app_id: str
    description: Any
    copyright: Any
    privacy_policy: Any
    custom_disclaimer: str
    categories: list[str]
    position: int
    is_listed: bool


class RecommendedAppsResultDict(TypedDict):
    recommended_apps: list[RecommendedAppItemDict]
    categories: list[str]


class RecommendedAppDetailDict(TypedDict):
    id: str
    name: str
    icon: Any
    icon_background: str | None
    mode: str
    export_data: str


class DatabaseRecommendAppRetrieval(RecommendAppRetrievalBase):
    """
    Retrieval recommended app from database
    """

    def get_recommended_apps_and_categories(self, language: str) -> RecommendedAppsResultDict:
        result = self.fetch_recommended_apps_from_db(language)
        return result

    def get_recommend_app_detail(self, app_id: str) -> RecommendedAppDetailDict | None:
        result = self.fetch_recommended_app_detail_from_db(app_id)
        return result

    def get_type(self) -> str:
        return RecommendAppType.DATABASE

    @classmethod
    def fetch_recommended_apps_from_db(cls, language: str) -> RecommendedAppsResultDict:
        """
        Fetch recommended apps from db.
        :param language: language
        :return:
        """
        recommended_apps = db.session.scalars(
            select(RecommendedApp).where(RecommendedApp.is_listed == True, RecommendedApp.language == language)
        ).all()

        if len(recommended_apps) == 0:
            recommended_apps = db.session.scalars(
                select(RecommendedApp).where(RecommendedApp.is_listed == True, RecommendedApp.language == languages[0])
            ).all()

        # -------------- extend start: add category to categories ---------------
        # fork 的应用中心分类来自自建表（RecommendedCategoryExtend / RecommendedAppsCategoryJoinExtend），
        # 不使用上游 1.14.2 原生的 RecommendedApp.categories（原生化迁移属 p3 范围）
        class_dick: dict[str, str] = {}
        recommended: dict[str, list[str]] = {}
        categories = set()
        recommended_apps_result: list[RecommendedAppItemDict] = []
        for item in db.session.query(RecommendedCategoryExtend).all():
            class_dick[item.id] = item.table
            categories.add(item.table)
        for like in db.session.query(RecommendedAppsCategoryJoinExtend).all():
            if like.recommended_id in recommended:
                recommended[like.recommended_id].append(like.category_id)
            else:
                recommended[like.recommended_id] = [like.category_id]
        for recommended_app in recommended_apps:
            classList = []
            app = recommended_app.app
            if not app or not app.is_public:
                continue
            description = app.description

            site = app.site
            if not site:
                continue

            config = app.app_model_config
            if config is not None and config.pre_prompt is not None and len(config.pre_prompt) > 0:
                description = config.pre_prompt
            if recommended_app.id in recommended:
                classList = recommended[recommended_app.id]
            if len(classList) == 0:
                classList.append("")
            app_categories = []
            for classId in classList:
                category = "未分类"
                if classId in class_dick:
                    category = class_dick[classId]
                app_categories.append(category)
                categories.add(category)  # add category to categories
            recommended_app_result: RecommendedAppItemDict = {
                "id": recommended_app.id,
                "app": recommended_app.app,
                "app_id": recommended_app.app_id,
                "description": description,
                "copyright": site.copyright,
                "privacy_policy": site.privacy_policy,
                "custom_disclaimer": site.custom_disclaimer,
                "categories": app_categories,
                "position": recommended_app.position,
                "is_listed": recommended_app.is_listed,
            }
            recommended_apps_result.append(recommended_app_result)

        categories.add("未分类")
        return RecommendedAppsResultDict(
            recommended_apps=recommended_apps_result,
            categories=sorted(categories),
        )
        # -------------- extend stop: add category to categories ---------------

    @classmethod
    def fetch_recommended_app_detail_from_db(cls, app_id: str) -> RecommendedAppDetailDict | None:
        """
        Fetch recommended app detail from db.
        :param app_id: App ID
        :return:
        """
        # is in public recommended list
        recommended_app = db.session.scalar(
            select(RecommendedApp).where(RecommendedApp.is_listed == True, RecommendedApp.app_id == app_id).limit(1)
        )

        if not recommended_app:
            return None

        # get app detail
        app_model = db.session.get(App, app_id)
        if not app_model or not app_model.is_public:
            return None

        return RecommendedAppDetailDict(
            id=app_model.id,
            name=app_model.name,
            icon=app_model.icon,
            icon_background=app_model.icon_background,
            mode=app_model.mode,
            export_data=AppDslService.export_dsl(app_model=app_model),
        )
