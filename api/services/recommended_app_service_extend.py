from flask_login import current_user

from extensions.ext_database import db
from models.model import (
    App,
    AppStatisticsExtend,  # Extend: App Center - Recommended list sorted by usage frequency
    InstalledApp,
    RecommendedApp,
    RecommendedAppsCategoryJoinExtend,
    RecommendedCategoryExtend,
    Tag,
    TagBinding,
)
from services.account_service_extend import TenantExtendService


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
                classList.append(type('Tag', (), {'name': '未分类'})())
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
        recommended: RecommendedApp = db.session.query(RecommendedApp).filter(RecommendedApp.app_id == app).first()
        db.session.query(RecommendedAppsCategoryJoinExtend).filter(
            RecommendedAppsCategoryJoinExtend.recommended_id == recommended.id
        ).delete()
        db.session.query(RecommendedApp).filter(RecommendedApp.app_id == app).delete()
        db.session.commit()

    @classmethod
    def sync_recommended_app(cls, app: str) -> str:
        # The role of the current user in the ta table must be admin or owner
        tenant_extend_service = TenantExtendService
        super_admin_id = tenant_extend_service.get_super_admin_id().id
        if super_admin_id != current_user.id:
            return ""
        try:
            # query application information
            recommendedApp = None
            appInfo: App = db.session.query(App).filter(App.id == app).first()
            appInfo.is_public = True
            db.session.commit()
            try:
                recommendedApp = db.session.query(RecommendedApp).filter(RecommendedApp.app_id == app).first()
            except:
                # create
                pass
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
            # query related tags
            tagList = []
            newList = []
            tagIdDick = {}
            tagNameDick = {}
            bindings = db.session.query(TagBinding).filter(TagBinding.target_id == appInfo.id).all()
            tag_ids = [binding.tag_id for binding in bindings]
            # get application type
            for recommended in db.session.query(RecommendedCategoryExtend).all():
                tagNameDick[recommended.tag_id] = recommended.table
                tagIdDick[recommended.tag_id] = recommended.id
            # query old associated data
            likes = (
                db.session.query(RecommendedAppsCategoryJoinExtend)
                .filter(RecommendedAppsCategoryJoinExtend.recommended_id == recommendedApp.id)
                .all()
            )
            categoryList = [like.category_id for like in likes]
            # query all
            if tag_ids:
                tags = db.session.query(Tag).filter(Tag.id.in_(tag_ids)).all()
                for tag in tags:
                    tagName = str.strip(tag.name)
                    if tag.id not in tagNameDick:
                        # create tag
                        classInfo = RecommendedCategoryExtend(
                            tag_id=tag.id,
                            table=tagName,
                        )
                        db.session.add(classInfo)
                    else:
                        classInfo = (
                            db.session.query(RecommendedCategoryExtend)
                            .filter(RecommendedCategoryExtend.tag_id == tag.id)
                            .first()
                        )
                        if tagNameDick[tag.id] != tagName:
                            classInfo.name = tagName
                    db.session.commit()
                    categoryId = classInfo.id
                    # Store new category id
                    newList.append(categoryId)
                    if tagName not in tagList:
                        tagList.append(tagName)
                    # do you have any old bindings
                    if categoryId not in categoryList:
                        # do you have tag permission?
                        db.session.add(
                            RecommendedAppsCategoryJoinExtend(
                                recommended_id=recommendedApp.id,
                                category_id=categoryId,
                            )
                        )
                        db.session.commit()
            # loop through an old type list
            for item in categoryList:
                if item not in newList:
                    db.session.query(RecommendedAppsCategoryJoinExtend).filter(
                        RecommendedAppsCategoryJoinExtend.recommended_id == recommendedApp.id,
                        RecommendedAppsCategoryJoinExtend.category_id == item,
                    ).delete()
            db.session.commit()
            return recommendedApp.id
        except:
            return ""

    # Extend: start messages context handling
    @classmethod
    def message_context(cls, conversation_id: str):
        from models.model_extend import MessageContextExtend
        message_list = []
        message_context = db.session.query(MessageContextExtend).filter(
            MessageContextExtend.conversation_id == conversation_id).order_by(
            MessageContextExtend.created_at.desc()).all()
        for v in message_context:
            message_list.append(v.message_id)
        return message_list

    @classmethod
    def delete_message_context(cls, conversation_id, message_id: str):
        from models.model_extend import MessageContextExtend
        db.session.query(MessageContextExtend).filter(
            MessageContextExtend.conversation_id == conversation_id,
            MessageContextExtend.message_id == message_id,
        ).delete()
        db.session.commit()
        return 'ok'
    # Extend: stop messages context handling
