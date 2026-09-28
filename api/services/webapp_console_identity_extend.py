"""Console identity for built-in WebApps, isolated from WebApp bearer tokens."""

import logging

from flask import Request
from sqlalchemy.orm import Session

from libs.passport import PassportService
from libs.token import extract_console_cookie_token
from models.account import Account
from services.account_service import AccountService

logger = logging.getLogger(__name__)


def get_console_account_extend(request: Request, *, session: Session) -> Account | None:
    """Resolve an admitted Console cookie without binding or changing an EndUser."""
    token = extract_console_cookie_token(request)
    if not token:
        return None
    try:
        claims = PassportService().verify(token)
        if claims.get("sub") != "Console API Passport" or not claims.get("exp"):
            return None
        account_id = claims.get("user_id")
        if not isinstance(account_id, str) or not account_id:
            return None
        # Upstream load_user commits activity/tenant changes and closes its Session.
        # Keep those effects away from the caller's WebApp transaction and EndUser.
        with Session(bind=session.get_bind(), expire_on_commit=False) as identity_session:
            return AccountService.load_logged_in_account(account_id=account_id, session=identity_session)
    except Exception:
        logger.debug("Console session could not be admitted for built-in WebApp", exc_info=True)
        return None
