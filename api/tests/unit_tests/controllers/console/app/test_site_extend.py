"""The fork switch must be explicit and kept out of the upstream Site field inventory."""

from dataclasses import fields

import pytest
from pydantic import ValidationError

from controllers.console.app.site import AppSiteResponse, AppSiteUpdatePayload
from services.app_site_service import AppSiteChanges
from services.app_site_service_extend import AppSiteChangesExtend


@pytest.mark.parametrize("payload", [{}, {"webapp_auth_enabled_extend": None}])
def test_missing_or_null_switch_preserves_upstream_change_type(payload):
    assert type(AppSiteUpdatePayload(**payload).to_changes()) is AppSiteChanges


@pytest.mark.parametrize("value", [False, True])
def test_switch_is_carried_only_in_fork_change_type(value):
    changes = AppSiteUpdatePayload(title="Site", webapp_auth_enabled_extend=value).to_changes()
    assert isinstance(changes, AppSiteChangesExtend)
    assert changes.webapp_auth_enabled_extend is value
    assert changes.title == "Site"
    assert "webapp_auth_enabled_extend" not in {field.name for field in fields(AppSiteChanges)}
    assert "webapp_auth_enabled_extend" not in AppSiteResponse.model_fields


@pytest.mark.parametrize("value", [0, 1, "true", "false", "", [], {}])
def test_switch_rejects_non_boolean_inputs(value):
    with pytest.raises(ValidationError):
        AppSiteUpdatePayload(webapp_auth_enabled_extend=value)
