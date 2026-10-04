"""Independent public-boundary checks for Casdoor Redis EVAL telemetry."""

import socket
from unittest.mock import Mock

import pytest
from core.casdoor.auth_transactions import (
    CREATE_SCRIPT,
    AuthTransactionError,
    AuthTransactionStore,
)
from libs import sensitive_redis
from opentelemetry import context as otel_context

SECRET = "independent-casdoor-token-913a"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    denied = Mock(
        side_effect=AssertionError("network forbidden in isolated verification")
    )
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield
    denied.assert_not_called()


def store_for(client):
    result = object.__new__(AuthTransactionStore)
    result._redis = client
    return result


def test_eval_preserves_call_identity_and_invokes_once():
    script = "return ARGV[1]"
    key = "auth-key"
    args = (SECRET, "digest")
    answer = object()
    redis = Mock()
    redis.eval.return_value = answer
    assert store_for(redis)._eval(script, (key,), *args) is answer
    redis.eval.assert_called_once_with(script, 1, key, *args)


@pytest.mark.parametrize(
    "failure", [OSError, ValueError, BaseException, KeyboardInterrupt, SystemExit]
)
def test_storage_errors_do_not_leak_or_retry(failure, caplog):
    redis = Mock()

    def fail(*_args):
        try:
            raise RuntimeError(SECRET)
        except RuntimeError as raw:
            raise failure(SECRET) from raw

    redis.eval.side_effect = fail
    expected_type = (
        failure if failure in (KeyboardInterrupt, SystemExit) else AuthTransactionError
    )
    with pytest.raises(expected_type) as caught:
        store_for(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    error = caught.value
    assert error.__cause__ is None and error.__context__ is None
    assert SECRET not in str(error)
    if expected_type is AuthTransactionError:
        assert str(error) == "casdoor_transaction_storage_uncertain"
    elif expected_type is SystemExit:
        assert error.code == 1
    assert SECRET not in caplog.text
    redis.eval.assert_called_once()


def test_privacy_guard_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(
        sensitive_redis, "version", Mock(side_effect=ImportError(SECRET))
    )
    redis = Mock()
    previous = otel_context.get_current()
    with pytest.raises(AuthTransactionError) as caught:
        store_for(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert str(caught.value) == "casdoor_transaction_storage_uncertain"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert otel_context.get_current() is previous
    redis.eval.assert_not_called()


def test_ineffective_telemetry_suppression_fails_closed(monkeypatch):
    from contextlib import nullcontext

    from opentelemetry.instrumentation import utils as otel_utils

    monkeypatch.setattr(otel_utils, "suppress_instrumentation", nullcontext)
    redis = Mock()
    with pytest.raises(AuthTransactionError) as caught:
        store_for(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert str(caught.value) == "casdoor_transaction_storage_uncertain"
    assert caught.value.__cause__ is caught.value.__context__ is None
    redis.eval.assert_not_called()


@pytest.mark.parametrize("cause", [KeyboardInterrupt, SystemExit, RuntimeError])
@pytest.mark.parametrize("teardown", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_teardown_error_does_not_displace_primary_eval_outcome(
    monkeypatch, cause, teardown
):
    from contextlib import contextmanager

    import sentry_sdk.scope

    real_scope = sentry_sdk.scope.use_scope

    @contextmanager
    def exits_badly(scope):
        with real_scope(scope):
            yield
        raise teardown(SECRET)

    monkeypatch.setattr(sentry_sdk.scope, "use_scope", exits_badly)
    redis = Mock()
    redis.eval.side_effect = cause(SECRET)
    expected_type = (
        cause if cause in (KeyboardInterrupt, SystemExit) else AuthTransactionError
    )
    with pytest.raises(expected_type) as caught:
        store_for(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert SECRET not in str(caught.value)
    redis.eval.assert_called_once()
