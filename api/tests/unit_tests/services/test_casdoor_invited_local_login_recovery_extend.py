"""Private recovery capability rejection and guarded actual shared-tail regressions."""

from types import SimpleNamespace

import pytest
from services.casdoor_invited_local_login_coordinator_service_extend import (
    _InvitedRecoveryContinuation,
    _InvitedSessionTail,
)
from test_casdoor_invited_local_login_coordinator_extend import invited as invited
from test_casdoor_invited_local_login_coordinator_extend import invited_session as original_invited_session
from test_casdoor_invited_local_login_coordinator_extend import chain as chain
from test_casdoor_invited_local_login_coordinator_extend import local_fixture as local_fixture
from test_casdoor_invited_local_login_coordinator_extend import login_env as login_env
from test_casdoor_invited_local_login_coordinator_extend import signing as signing

invited_session = original_invited_session


@pytest.mark.parametrize("kind", ["projection", "recovery", "tail"])
def test_read_only_or_manual_projection_cannot_enter_recovery_tail(invited_session, kind):
    caller = invited_session.caller
    if kind == "projection":
        value = SimpleNamespace(caller=caller)
    elif kind == "recovery":
        value = object.__new__(_InvitedRecoveryContinuation)
        object.__setattr__(value, "caller", caller)
    else:
        value = object.__new__(_InvitedSessionTail)
        object.__setattr__(value, "caller", caller)
    method = caller._finish_invited_session if kind == "tail" else caller._continue_invited_recovery
    with pytest.raises(Exception, match="invalid_invitation_recovery_continuation|invalid_invited_session_tail"):
        method(value)
    assert not invited_session.issuer.calls and not invited_session.chain.redis.data
