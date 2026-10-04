"""Private, invocation-bound evidence of a normal failure before storage entry.

The actual consumer starts this owner only after its claim commit and clean close.
Only direct normal returns from the original fetch/image owners can seal evidence.
Opaque identity registration rejects constructed, copied and replayed capabilities.
This is no-object-created-by-this-invocation evidence, never storage absence,
remote quiescence, deletion, retry authority or a successful public result.
"""

from dataclasses import dataclass
from uuid import uuid4
from weakref import WeakKeyDictionary, ref

import httpx
from services.file_service import AvatarNormalization, FileService

from core.file import remote_fetcher

_FETCH_OWNER = remote_fetcher.fetch_bounded_external_file
_IMAGE_OWNER = FileService.normalize_avatar_image
_REJECTED_REASONS = frozenset(
    {
        "invalid_arguments",
        "proxy_required",
        "privacy_unsupported",
        "runtime_unsupported",
        "invalid_url",
        "local_origin",
        "http_status",
        "encoded_response",
        "too_large",
        "proxy_denied",
    }
)


class _PreStorageInvocation:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("avatar_termination_private")


class _PreStorageCapability:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("avatar_termination_private")


@dataclass(frozen=True, repr=False)
class _PreStorageBinding:
    attempt: object
    source: object
    reservation: object
    reason: str
    proof_ref: object


@dataclass(repr=False)
class _State:
    invocation: object
    claim: object
    binding: tuple
    stage: str = "initial"
    storage_entered: bool = False
    result: object = None
    reason: str | None = None
    issued: object = None


_states = WeakKeyDictionary()
_capabilities = WeakKeyDictionary()
_committed_claims = {}


def _register_committed_avatar_claim(invocation, result):
    """Called by the actual root owner after acknowledged commit and clean close.

    Identity registration excludes manually constructed/copied result and claim
    DTOs. No Session, crypto, source plaintext or provider response is retained.
    """
    from repositories.casdoor_avatar_repository_extend import _AvatarClaim

    if (
        result.committed
        and result.commit_attempted
        and result.clean
        and not result.failed
        and invocation.signal is None
        and type(result.value) is _AvatarClaim
        and result.value.code == "reserved"
    ):
        key = id(result)
        _committed_claims[key] = (
            ref(result, lambda _reference: _committed_claims.pop(key, None)),
            ref(invocation),
            result.value,
        )


def _start_pre_storage(invocation, claimed, authority):
    """Bind original acknowledged/closed claim, never a claim field projection."""
    from repositories.casdoor_avatar_repository_extend import (
        _AvatarClaim,
        _AvatarIOAuthority,
    )
    from services.casdoor_avatar_consumer_service_extend import _Invocation, _RootResult

    if (
        type(invocation) is not _Invocation
        or invocation.signal is not None
        or type(claimed) is not _RootResult
        or not claimed.committed
        or not claimed.commit_attempted
        or not claimed.clean
        or claimed.failed
        or type(claimed.value) is not _AvatarClaim
        or claimed.value.code != "reserved"
        or type(authority) is not _AvatarIOAuthority
        or (authority.attempt, authority.source, authority.reservation)
        != (claimed.value.attempt, claimed.value.source, claimed.value.reservation)
    ):
        raise ValueError("avatar_termination_invalid")
    registered = _committed_claims.pop(id(claimed), None)
    if (
        registered is None
        or registered[0]() is not claimed
        or registered[1]() is not invocation
        or registered[2] is not claimed.value
    ):
        raise ValueError("avatar_termination_invalid")
    claim = claimed.value
    tracker = object.__new__(_PreStorageInvocation)
    _states[tracker] = _State(invocation, claim, (claim.attempt, claim.source, claim.reservation))
    return tracker


def _state(tracker):
    if type(tracker) is not _PreStorageInvocation:
        raise ValueError("avatar_termination_invalid")
    state = _states.get(tracker)
    if (
        state is None
        or state.invocation.signal is not None
        or (state.claim.attempt, state.claim.source, state.claim.reservation) != state.binding
    ):
        raise ValueError("avatar_termination_invalid")
    return state


def _fetch_before_storage(tracker, supplier, budget):
    """Observe the exact normal owner result and post-return deadline check."""
    state = _state(tracker)
    if state.stage != "initial" or state.storage_entered:
        raise ValueError("avatar_termination_invalid")
    state.stage = "fetching"
    owner = remote_fetcher.fetch_bounded_external_file
    supplier_timed_out = False

    def local_supplier():
        nonlocal supplier_timed_out
        try:
            return supplier()
        except (TimeoutError, httpx.TimeoutException):
            # A negative-only veto before the owner erases the exception type.
            # No exception object or provider details leave its private scope.
            supplier_timed_out = True
            raise

    try:
        result = owner(local_supplier, budget.ceiling)
        budget.check()
    except BaseException:
        state.stage = "uncertain"
        raise
    state.result = result
    state.stage = "fetched"
    if (
        owner is _FETCH_OWNER
        and type(result) is remote_fetcher.BoundedExternalFile
        and result.termination == "confirmed"
        and not supplier_timed_out
        and (
            (result.status == "rejected" and result.reason in _REJECTED_REASONS)
            or (result.status == "failed" and result.reason == "supplier_failed")
        )
    ):
        state.reason = "fetch_rejected" if result.status == "rejected" else "fetch_failed"
        state.stage = "rejected"
    return result


def _normalize_before_storage(tracker, fetched, budget):
    """A caller-supplied result/copy cannot substitute for the observed fetch."""
    state = _state(tracker)
    if (
        state.stage != "fetched"
        or state.storage_entered
        or state.result is not fetched
        or type(fetched) is not remote_fetcher.BoundedExternalFile
        or (fetched.status, fetched.reason, fetched.termination) != ("ok", "ok", "confirmed")
    ):
        raise ValueError("avatar_termination_invalid")
    state.stage = "normalizing"
    state.result = None
    owner = FileService.normalize_avatar_image
    try:
        result = owner(fetched.content)
        budget.check()
    except BaseException:
        state.stage = "uncertain"
        raise
    state.result = result
    state.stage = "normalized"
    if (
        owner is _IMAGE_OWNER
        and type(result) is AvatarNormalization
        and result.code in ("invalid_image", "unsupported_image_codec")
        and result.image is None
    ):
        state.reason = "image_rejected"
        state.stage = "rejected"
    return result


def _enter_avatar_storage(tracker):
    """Irrevocable marker immediately before the actual storage owner invocation."""
    state = _state(tracker)
    state.storage_entered = True
    state.stage = "storage_entered"
    state.result = state.reason = None
    if state.issued is not None:
        _capabilities.pop(state.issued, None)


def _seal_pre_storage(tracker):
    """No reason/result argument: only the locally observed owner can issue once."""
    if tracker is None:
        return None
    try:
        state = _state(tracker)
    except ValueError:
        return None
    if state.storage_entered or state.stage != "rejected" or state.issued is not None:
        return None
    capability = object.__new__(_PreStorageCapability)
    attempt, source, reservation = state.binding
    binding = _PreStorageBinding(attempt, source, reservation, state.reason, uuid4())
    _capabilities[capability] = (ref(tracker), binding)
    state.issued = capability
    state.stage = "sealed"
    state.result = None
    return capability


def _consume_pre_storage(capability, attempt):
    """Consume identity once, including a failed SQL attempt; never reissue."""
    if type(capability) is not _PreStorageCapability:
        raise ValueError("avatar_termination_invalid")
    registered = _capabilities.pop(capability, None)
    if registered is None:
        raise ValueError("avatar_termination_invalid")
    tracker_ref, binding = registered
    state = _state(tracker_ref())
    if (
        state.storage_entered
        or state.stage != "sealed"
        or state.issued is not capability
        or binding.attempt is not attempt
        or (binding.attempt, binding.source, binding.reservation) != state.binding
    ):
        raise ValueError("avatar_termination_invalid")
    state.stage = "consumed"
    return binding
