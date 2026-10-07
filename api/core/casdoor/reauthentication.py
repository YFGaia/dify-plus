"""Pure signed authentication-time observation; no session or action authority."""

import math
from dataclasses import dataclass
from datetime import datetime

from core.casdoor.claims import ClaimsError, ClaimsValidator


@dataclass(frozen=True, repr=False)
class RecentAuthTimeObservation:
    """Signed timestamp only, not session proof or an authorization/admission decision.

    This is neither unlink authorization nor a replay-resistant action capability.
    """

    auth_time: int | float

    def __repr__(self) -> str:
        return "RecentAuthTimeObservation(<redacted>)"


def verify_recent_auth_time(
    validator: ClaimsValidator,
    id_token: str,
    *,
    expected_nonce: str,
    auth_started_at: datetime,
    now: datetime,
) -> RecentAuthTimeObservation:
    """Observe a verified signed NumericDate inside both recent-auth windows.

    The caller supplies a trusted transaction start and a single UTC clock.
    This observation is not source-session proof, unlink authorization, an
    admission decision or a replay-resistant action capability.
    """
    failure = None
    try:
        # The ordinary API permits an omitted clock; this helper requires one.
        if now is None:
            raise ClaimsError("clock_invalid")
        validator.verify_id_token(id_token, expected_nonce=expected_nonce, auth_started_at=auth_started_at, now=now)
        claims = validator._token(id_token, required=["auth_time"], now=now)
        observed = claims["auth_time"]
        if type(observed) not in (int, float):
            raise ClaimsError("auth_time_invalid")
        try:
            finite = math.isfinite(observed)
        except OverflowError:
            finite = False
        if (
            not finite
            or not max(auth_started_at.timestamp() - 60, now.timestamp() - 300) <= observed <= now.timestamp()
        ):
            raise ClaimsError("auth_time_invalid")
    except ClaimsError as error:
        failure = (error.reason, error.code)
    # Rebuild outside the handler: upstream parser/SDK errors can retain private
    # provider text in __context__ even when their display uses `from None`.
    if failure is not None:
        raise ClaimsError(*failure) from None
    return RecentAuthTimeObservation(observed)
