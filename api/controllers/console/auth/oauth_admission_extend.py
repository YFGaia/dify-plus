"""Keep configured OAuth2 SSO independent from the GitHub/Google login switch."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask_restx import Resource

from controllers.console.wraps import social_oauth_login_enabled


def oauth_login_enabled_extend(view: Callable[..., Any]) -> Callable[..., Any]:
    """OAuth2 is admitted by its active integration; social providers retain upstream policy."""
    social_view = social_oauth_login_enabled(view)

    @wraps(view)
    def decorated(resource: Resource, provider: str, *args: Any, **kwargs: Any) -> Any:
        selected_view = view if provider == "oauth2" else social_view
        return selected_view(resource, provider, *args, **kwargs)

    return decorated
