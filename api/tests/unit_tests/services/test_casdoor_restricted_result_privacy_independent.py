"""Independent checks at the restricted-result Redis privacy boundary."""

import pytest
from core.casdoor import auth_transactions as auth
from core.casdoor.auth_transactions import AuthTransactionError
from test_casdoor_local_http_service_extend import begin, complete

pytest_plugins = ("test_casdoor_restricted_result_service_extend",)

PRIVATE = "independent-private-result-baseexception-marker"


def test_custom_baseexception_inside_privacy_call_is_fixed_and_context_free(http_flow, monkeypatch, caplog):
    flow = http_flow

    class PrivateFailure(BaseException):
        pass

    primary = PrivateFailure(PRIVATE)
    original = flow.init.eval
    before = len([call for call in flow.init.calls if call[0] == auth.CREATE_RESTRICTED_RESULT_SCRIPT])

    def evaluate(script, count, *args):
        result = original(script, count, *args)
        if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
            raise primary
        return result

    monkeypatch.setattr(flow.init, "eval", evaluate)
    store = flow.service._store()
    with pytest.raises(AuthTransactionError) as caught:
        store._eval(auth.CREATE_RESTRICTED_RESULT_SCRIPT, ("independent-private-key",), "private-value")

    error = caught.value
    assert error.reason == "storage_uncertain"
    assert str(error) == "casdoor_transaction_storage_uncertain"
    assert error.__cause__ is None and error.__context__ is None
    assert PRIVATE not in repr((error.reason, str(error), error.__cause__, error.__context__, caplog.text))
    assert type(primary).__name__ not in caplog.text
    assert len([call for call in flow.init.calls if call[0] == auth.CREATE_RESTRICTED_RESULT_SCRIPT]) == before + 1


def test_keyboardinterrupt_and_systemexit_keep_control_flow_signals(http_flow, monkeypatch):
    flow = http_flow
    original = flow.init.eval
    store = flow.service._store()
    call_count = 0

    for index, signal_type in enumerate((KeyboardInterrupt, SystemExit)):
        primary = signal_type(PRIVATE)

        def evaluate(script, count, *args, _primary=primary):
            nonlocal call_count
            result = original(script, count, *args)
            if script == auth.CREATE_RESTRICTED_RESULT_SCRIPT:
                call_count += 1
                raise _primary
            return result

        monkeypatch.setattr(flow.init, "eval", evaluate)
        with pytest.raises(signal_type) as caught:
            store._eval(
                auth.CREATE_RESTRICTED_RESULT_SCRIPT,
                (f"independent-control-flow-{index}",),
                "private-value",
            )

        assert type(caught.value) is signal_type
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        if signal_type is KeyboardInterrupt:
            assert str(caught.value) == "sensitive Redis call interrupted"
        else:
            assert caught.value.code == 1
        assert PRIVATE not in str(caught.value)

    assert call_count == 2


def test_baseexception_outside_privacy_context_keeps_identity_through_failed_finish(http_flow, monkeypatch):
    flow = http_flow
    scope, _ = begin(flow)

    class Stop(BaseException):
        pass

    primary = Stop("private outside-context cancellation")

    def stop(*args):
        raise primary

    flow.control.fail_close = True
    monkeypatch.setattr(type(flow.service), "_directory_inputs", stop)
    with pytest.raises(Stop) as caught:
        complete(flow, scope)

    assert caught.value is primary
    assert primary._casdoor_clear_cookies == (flow.control.consumed[-1].clear_cookie,)
    assert flow.control.runtime_scopes[-1].finish_calls == 1
