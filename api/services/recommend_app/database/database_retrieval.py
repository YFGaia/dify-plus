from typing import Optional

from constants.languages import languages
from extensions.ext_database import db
from models.model import (  # extend add category to categories
    App,
    RecommendedApp,
    RecommendedAppsCategoryJoinExtend,
    RecommendedCategoryExtend,
)
from services.app_dsl_service import AppDslService
from services.recommend_app.recommend_app_base import RecommendAppRetrievalBase
from services.recommend_app.recommend_app_type import RecommendAppType


class DatabaseRecommendAppRetrieval(RecommendAppRetrievalBase):
    """
    Retrieval recommended app from database
    """

    def get_recommended_apps_and_categories(self, language: str) -> dict:
        result = self.fetch_recommended_apps_from_db(language)
        return result

    def get_recommend_app_detail(self, app_id: str):
        result = self.fetch_recommended_app_detail_from_db(app_id)
        return result

    def get_type(self) -> str:
        return RecommendAppType.DATABASE

    @classmethod
    def fetch_recommended_apps_from_db(cls, language: str) -> dict:
        """
        Fetch recommended apps from db.
        :param language: language
        :return:
        """
        recommended_apps = (
            db.session.query(RecommendedApp)
            .where(RecommendedApp.is_listed == True, RecommendedApp.language == language)
            .all()
        )

        if len(recommended_apps) == 0:
            recommended_apps = (
                db.session.query(RecommendedApp)
                .where(RecommendedApp.is_listed == True, RecommendedApp.language == languages[0])
                .all()
            )

        # -------------- extend start: add category to categories ---------------
        tag_i = 0
        class_dick = {}
        recommended = {}
        categories = set()
        recommended_apps_result = []
        for item in db.session.query(RecommendedCategoryExtend).all():
            class_dick[item.id] = item.table
            categories.add(item.table)
            tag_i += 1
        for like in db.session.query(RecommendedAppsCategoryJoinExtend).all():
            if like.recommended_id in recommended:
                recommended[like.recommended_id].append(like.category_id)
            else:
                recommended[like.recommended_id] = [like.category_id]
        for recommended_app in recommended_apps:
            classList = []
            app = recommended_app.app
            description = app.description
            if not app or not app.is_public:
                continue

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
            for classId in classList:
                category = "未分类"
                if classId in class_dick:
                    category = class_dick[classId]
                recommended_app_result = {
                    "id": recommended_app.id,
                "app": recommended_app.app,
                    "app_id": recommended_app.app_id,
                    "description": description,
                    "copyright": site.copyright,
                    "privacy_policy": site.privacy_policy,
                    "custom_disclaimer": site.custom_disclaimer,
                    "category": category,
                    "position": recommended_app.position,
                    "is_listed": recommended_app.is_listed,
                }
                recommended_apps_result.append(recommended_app_result)

                categories.add(recommended_app.category)  # add category to categories
        categories = sorted(categories)
        categories.append("未分类")
        return {"recommended_apps": recommended_apps_result, "categories": categories}
        # -------------- extend stop: add category to categories ---------------

    @classmethod
    def fetch_recommended_app_detail_from_db(cls, app_id: str) -> Optional[dict]:
        """
        Fetch recommended app detail from db.
        :param app_id: App ID
        :return:
        """
        # is in public recommended list
        recommended_app = (
            db.session.query(RecommendedApp)
            .where(RecommendedApp.is_listed == True, RecommendedApp.app_id == app_id)
            .first()
        )

        if not recommended_app:
            return None

        # get app detail
        app_model = db.session.query(App).where(App.id == app_id).first()
        if not app_model or not app_model.is_public:
            return None

        return {
            "id": app_model.id,
            "name": app_model.name,
            "icon": app_model.icon,
            "icon_background": app_model.icon_background,
            "mode": app_model.mode,
            "export_data": AppDslService.export_dsl(app_model=app_model),
        }
