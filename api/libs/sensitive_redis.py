"""Call-local telemetry suppression for synchronous Redis operations.

SDK imports stay lazy. Unsupported instrumentation fails closed; callers must
translate uncertainty into their own fixed error and must never retry here.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from importlib.metadata import version


class SensitiveRedisError(Exception):
    """Privacy setup, operation, or teardown could not be confirmed."""


@contextmanager
def sensitive_redis_call() -> Iterator[None]:
    """Suppress installed Sentry and OTel Redis instrumentation for one call.

    Fresh scopes hide inherited spans and integrations without changing the global
    client. The sole caller must sanitize exceptions after leaving this context:
    Python can reattach a thrown body exception during generator context exit.
    Failures and interrupts are converted to fixed errors or control-flow signals.
    """
    signal = None
    try:
        if (
            version("sentry-sdk") != "2.57.0"
            or version("opentelemetry-instrumentation") != "0.65b0"
            or version("opentelemetry-instrumentation-redis") != "0.65b0"
        ):
            raise SensitiveRedisError("sensitive_redis_uncertain")
        import sentry_sdk
        from opentelemetry import context as otel_context
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled, suppress_instrumentation
        from sentry_sdk.client import BaseClient
        from sentry_sdk.scope import use_isolation_scope, use_scope

        class PrivateClient(BaseClient):
            def is_active(self) -> bool:
                # Prevent Scope.get_client from falling back to the global client.
                return True

        previous_current = sentry_sdk.get_current_scope()
        previous_isolation = sentry_sdk.get_isolation_scope()
        previous_otel = otel_context.get_current()
        private_client = PrivateClient()
        with ExitStack() as contexts:
            contexts.enter_context(suppress_instrumentation())
            current = sentry_sdk.Scope()
            isolation = sentry_sdk.Scope()
            # Scope(client=...) / set_client also mutate SDK globals in 2.57.0.
            current.client = isolation.client = private_client
            contexts.enter_context(use_isolation_scope(isolation))
            contexts.enter_context(use_scope(current))
            if (
                is_instrumentation_enabled()
                or sentry_sdk.get_client() is not private_client
                or sentry_sdk.get_current_scope() is not current
                or sentry_sdk.get_isolation_scope() is not isolation
                or current.span is not None
                or isolation.span is not None
            ):
                raise SensitiveRedisError("sensitive_redis_uncertain")
            try:
                yield
            except KeyboardInterrupt:
                signal = "interrupt"
            except SystemExit:
                signal = "exit"
            except BaseException:
                signal = "error"
            finally:
                current = isolation = None
        if (
            sentry_sdk.get_current_scope() is not previous_current
            or sentry_sdk.get_isolation_scope() is not previous_isolation
            or otel_context.get_current() is not previous_otel
        ):
            raise SensitiveRedisError("sensitive_redis_uncertain")
    except KeyboardInterrupt:
        if signal is None:
            signal = "interrupt"
    except SystemExit:
        if signal is None:
            signal = "exit"
    except BaseException:
        # Preserve a pending interrupt even if a teardown subsequently fails.
        if signal not in ("interrupt", "exit"):
            signal = "error"
    if signal == "interrupt":
        raise KeyboardInterrupt("sensitive Redis call interrupted") from None
    if signal == "exit":
        raise SystemExit(1) from None
    if signal == "error":
        raise SensitiveRedisError("sensitive_redis_uncertain") from None
