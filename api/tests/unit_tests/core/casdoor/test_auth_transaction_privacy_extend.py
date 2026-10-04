"""One eval call boundary only; no auth-flow or invitation-store execution."""

import socket
from contextlib import contextmanager, nullcontext
from unittest.mock import Mock

import pytest
import sentry_sdk.scope
from opentelemetry import context as otel_context
from opentelemetry.instrumentation import utils as otel_utils

from core.casdoor.auth_transactions import CREATE_SCRIPT, AuthTransactionError, AuthTransactionStore
from libs import sensitive_redis

SECRET = "synthetic-sensitive-auth-transaction-I28"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    denied = Mock(side_effect=AssertionError("offline test attempted network access"))
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield
    denied.assert_not_called()


def make_store(redis):
    # _eval has no dependency on constructor crypto/key setup.
    store = object.__new__(AuthTransactionStore)
    store._redis = redis
    return store


def test_exact_script_keys_arguments_and_result():
    redis = Mock()
    result = object()
    redis.eval.return_value = result
    assert make_store(redis)._eval(CREATE_SCRIPT, ("key", "index"), SECRET, "digest") is result
    redis.eval.assert_called_once_with(CREATE_SCRIPT, 2, "key", "index", SECRET, "digest")


@pytest.mark.parametrize(
    "failure", [RuntimeError, ValueError, TimeoutError, BaseException, KeyboardInterrupt, SystemExit]
)
def test_safe_error_and_interrupts_without_raw_chain(failure, caplog):
    def fail(*_args):
        raise failure(SECRET) from ValueError(SECRET)

    redis = Mock()
    redis.eval.side_effect = fail
    expected = failure if failure in (KeyboardInterrupt, SystemExit) else AuthTransactionError
    with pytest.raises(expected) as caught:
        make_store(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert SECRET not in str(caught.value)
    if expected is AuthTransactionError:
        assert caught.value.reason == "storage_uncertain"
        assert str(caught.value) == "casdoor_transaction_storage_uncertain"
    if expected is SystemExit:
        assert caught.value.code == 1
    assert SECRET not in caplog.text
    redis.eval.assert_called_once()


@pytest.mark.parametrize("phase", ["setup", "teardown"])
@pytest.mark.parametrize("failure", [RuntimeError, BaseException, KeyboardInterrupt, SystemExit])
def test_sdk_context_failures_are_safe(monkeypatch, phase, failure, caplog):
    original = sentry_sdk.scope.use_scope
    prior_current = sentry_sdk.get_current_scope()
    prior_isolation = sentry_sdk.get_isolation_scope()
    prior_otel = otel_context.get_current()

    @contextmanager
    def broken_scope(scope):
        if phase == "setup":
            raise failure(SECRET)
        with original(scope):
            yield
        raise failure(SECRET)

    monkeypatch.setattr(sentry_sdk.scope, "use_scope", broken_scope)
    redis = Mock()
    expected = failure if failure in (KeyboardInterrupt, SystemExit) else AuthTransactionError
    with pytest.raises(expected) as caught:
        make_store(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert redis.eval.call_count == (1 if phase == "teardown" else 0)
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert SECRET not in caplog.text
    assert sentry_sdk.get_current_scope() is prior_current
    assert sentry_sdk.get_isolation_scope() is prior_isolation
    assert otel_context.get_current() is prior_otel


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("teardown", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_teardown_cannot_replace_primary_failure(monkeypatch, failure, teardown):
    original = sentry_sdk.scope.use_scope

    @contextmanager
    def broken_scope(scope):
        with original(scope):
            yield
        raise teardown(SECRET)

    monkeypatch.setattr(sentry_sdk.scope, "use_scope", broken_scope)
    redis = Mock()
    redis.eval.side_effect = failure(SECRET)
    expected = failure if failure in (KeyboardInterrupt, SystemExit) else AuthTransactionError
    with pytest.raises(expected) as caught:
        make_store(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert SECRET not in str(caught.value)
    redis.eval.assert_called_once()


def test_missing_sdk_version_fails_before_redis(monkeypatch):
    monkeypatch.setattr(sensitive_redis, "version", Mock(side_effect=ImportError(SECRET)))
    redis = Mock()
    with pytest.raises(AuthTransactionError, match="^casdoor_transaction_storage_uncertain$") as caught:
        make_store(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    redis.eval.assert_not_called()
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "api", ["missing_suppression", "missing_scope", "ineffective_suppression", "ineffective_scope"]
)
def test_required_sdk_apis_fail_closed(monkeypatch, api):
    if api == "missing_suppression":
        monkeypatch.delattr(otel_utils, "suppress_instrumentation")
    elif api == "missing_scope":
        monkeypatch.delattr(sentry_sdk.scope, "use_scope")
    elif api == "ineffective_suppression":
        monkeypatch.setattr(otel_utils, "suppress_instrumentation", nullcontext)
    else:
        monkeypatch.setattr(sentry_sdk.scope, "use_scope", nullcontext)
    prior_current = sentry_sdk.get_current_scope()
    prior_isolation = sentry_sdk.get_isolation_scope()
    prior_otel = otel_context.get_current()
    redis = Mock()
    with pytest.raises(AuthTransactionError, match="^casdoor_transaction_storage_uncertain$") as caught:
        make_store(redis)._eval(CREATE_SCRIPT, ("key",), SECRET)
    redis.eval.assert_not_called()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentry_sdk.get_current_scope() is prior_current
    assert sentry_sdk.get_isolation_scope() is prior_isolation
    assert otel_context.get_current() is prior_otel
