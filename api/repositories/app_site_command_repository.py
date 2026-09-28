"""SQLAlchemy persistence adapter for Console app site management."""

from dataclasses import fields
from typing import override

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from libs.datetime_utils import naive_utc_now
from models.enums import AppStatus
from models.model import App, Site
from services.app_site_service import (
    AppSiteAppNotFoundError,
    AppSiteChanges,
    AppSiteCommandResult,
    AppSiteNotFoundError,
    AppSiteStore,
)
from services.app_site_service_extend import AppSiteChangesExtend
from services.webapp_auth_service_extend import WebAppAuthExtendService


class AppSiteCommandRepository(AppSiteStore):
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @override
    def update_site(
        self,
        *,
        workspace_id: str,
        app_id: str,
        actor_id: str,
        changes: AppSiteChanges,
    ) -> AppSiteCommandResult:
        with self._session_factory.begin() as session:
            site = self._get_site(session, workspace_id, app_id)
            for field in fields(AppSiteChanges):
                field_name = field.name
                value = getattr(changes, field_name)
                if value is not None:
                    setattr(site, field_name, value)

            auth_changed = isinstance(changes, AppSiteChangesExtend) and changes.webapp_auth_enabled_extend is not None
            if isinstance(changes, AppSiteChangesExtend) and changes.webapp_auth_enabled_extend is not None:
                WebAppAuthExtendService.set_webapp_auth_enabled(
                    app_id, changes.webapp_auth_enabled_extend, session=session
                )

            site.updated_by = actor_id
            site.updated_at = naive_utc_now()
            session.flush()
            result = self._to_command_result(site)
        if auth_changed:
            WebAppAuthExtendService.invalidate_after_commit(app_id)
        return result

    @override
    def reset_access_token(
        self,
        *,
        workspace_id: str,
        app_id: str,
        actor_id: str,
    ) -> AppSiteCommandResult:
        with self._session_factory.begin() as session:
            site = self._get_site(session, workspace_id, app_id)
            site.code = Site.generate_code(16, session=session)
            site.updated_by = actor_id
            site.updated_at = naive_utc_now()
            session.flush()
            return self._to_command_result(site)

    @staticmethod
    def _get_site(session: Session, workspace_id: str, app_id: str) -> Site:
        site = session.scalar(
            select(Site)
            .join(App, App.id == Site.app_id)
            .where(
                App.id == app_id,
                App.tenant_id == workspace_id,
                App.status == AppStatus.NORMAL,
            )
            .with_for_update(of=Site)
            .limit(1)
        )
        if site is not None:
            return site

        app_exists = session.scalar(
            select(App.id)
            .where(
                App.id == app_id,
                App.tenant_id == workspace_id,
                App.status == AppStatus.NORMAL,
            )
            .limit(1)
        )
        if app_exists is None:
            raise AppSiteAppNotFoundError
        raise AppSiteNotFoundError

    @staticmethod
    def _to_command_result(site: Site) -> AppSiteCommandResult:
        return AppSiteCommandResult(
            app_id=site.app_id,
            code=site.code,
            title=site.title,
            icon=site.icon,
            icon_background=site.icon_background,
            description=site.description,
            default_language=site.default_language,
            customize_domain=site.customize_domain,
            copyright=site.copyright,
            privacy_policy=site.privacy_policy,
            input_placeholder=site.input_placeholder,
            custom_disclaimer=site.custom_disclaimer,
            customize_token_strategy=str(site.customize_token_strategy),
            prompt_public=site.prompt_public,
            show_workflow_steps=site.show_workflow_steps,
            use_icon_as_answer_icon=site.use_icon_as_answer_icon,
        )
