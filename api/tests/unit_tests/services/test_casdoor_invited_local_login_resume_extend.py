"""No read-only/manual/old capability can enter original phase mutation."""

from types import SimpleNamespace

import pytest
from services.casdoor_invited_local_login_coordinator_service_extend import (
    _InvitedRecoveryContinuation,
    _InvitedRecoveryFinalizationGuard,
)
from test_casdoor_invited_local_login_coordinator_extend import invited as invited
from test_casdoor_invited_local_login_coordinator_extend import invited_session as original_invited_session
from test_casdoor_invited_local_login_coordinator_extend import chain as chain
from test_casdoor_invited_local_login_coordinator_extend import local_fixture as local_fixture
from test_casdoor_invited_local_login_coordinator_extend import login_env as login_env
from test_casdoor_invited_local_login_coordinator_extend import signing as signing


invited_session = original_invited_session


@pytest.mark.parametrize("kind", ["projection", "manual-continuation", "manual-guard"])
def test_unregistered_projection_never_enters_resume_or_finalizer(invited_session, kind):
    caller = invited_session.caller
    if kind == "projection":
        value = SimpleNamespace(caller=caller)
    else:
        value = object.__new__(
            _InvitedRecoveryContinuation if kind == "manual-continuation" else _InvitedRecoveryFinalizationGuard
        )
        object.__setattr__(value, "caller", caller)
    with pytest.raises(Exception, match="invalid_invitation_resume_continuation"):
        caller._continue_invited_resume(value)
    with pytest.raises(Exception, match="invitation_operation_unavailable"):
        caller._invitation_finalizer._finalize_invited_recovery(None, token="synthetic invalid bearer", caller_guard=value)
    assert not invited_session.issuer.calls and not invited_session.chain.redis.data
