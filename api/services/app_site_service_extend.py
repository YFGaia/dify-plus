"""Fork site changes kept separate from the upstream site field inventory."""

from dataclasses import dataclass

from services.app_site_service import AppSiteChanges


@dataclass(frozen=True, slots=True)
class AppSiteChangesExtend(AppSiteChanges):
    webapp_auth_enabled_extend: bool | None = None
