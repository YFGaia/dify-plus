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


_STORE_OWNER = FileService.store_reserved_avatar
_CLEANUP_FILE_OWNER = FileService._cleanup_reserved_avatar
_normal_storage = WeakKeyDictionary()
_cleanup_declarations = WeakKeyDictionary()
_cleanup_permits = WeakKeyDictionary()
_cleanup_observations = WeakKeyDictionary()


class _AvatarCleanupDeclaration:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("avatar_cleanup_private")


class _AvatarCleanupPermit:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("avatar_cleanup_private")


class _AvatarCleanupObservation:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("avatar_cleanup_private")


@dataclass(frozen=True, repr=False)
class _AvatarCleanupBinding:
    attempt: object
    source: object
    reservation: object
    proof_ref: object
    sha3_256: str
    size: int


@dataclass(repr=False)
class _NormalStorageObservation:
    binding: object
    domain: object
    invocation: object
    attached: object = None
    consumer: object = None
    declared: bool = False


def _store_avatar_observed(tracker, reservation, normalized, budget):
    """Call original normal store once; record only timely exact native fs success."""
    from core.casdoor.avatar_cleanup_provider import _native_avatar_domain

    state = _state(tracker)
    if state.stage != "normalized" or state.result.image is not normalized or reservation != state.claim.reservation:
        raise ValueError("avatar_cleanup_invalid")
    domain = _native_avatar_domain()
    owner = FileService.store_reserved_avatar
    _enter_avatar_storage(tracker)
    result = owner(reservation, normalized)
    budget.check()
    if (
        result == "stored"
        and owner is _STORE_OWNER
        and domain is not None
        and _native_avatar_domain() is domain
        and state.invocation.signal is None
    ):
        _normal_storage[tracker] = _NormalStorageObservation(
            _AvatarCleanupBinding(
                state.claim.attempt,
                state.claim.source,
                reservation,
                uuid4(),
                normalized.sha3_256,
                len(normalized.content),
            ),
            domain,
            state.invocation,
        )
    return result


def _attach_avatar_observed(tracker, consumer, attempt, normalized, budget):
    """Observe the actual original SQL root, including its independent rollback/close."""
    from services.casdoor_avatar_consumer_service_extend import CasdoorAvatarConsumerService, _AVATAR_ROOT_OWNER

    state = _state(tracker)
    if (
        type(consumer) is not CasdoorAvatarConsumerService
        or consumer._root.__func__ is not _AVATAR_ROOT_OWNER
        or attempt is not state.claim.attempt
    ):
        raise ValueError("avatar_cleanup_invalid")
    result = consumer._root(
        state.invocation, lambda repo: repo.recheck_and_attach(attempt, normalized, now=budget.check()), write=True
    )
    observation = _normal_storage.get(tracker)
    if observation is not None:
        observation.attached = result
        observation.consumer = consumer
    return result


def _consume_avatar_cleanup(declaration):
    """Identity consumed once, including failed SQL; a parsed audit cannot recreate it."""
    if type(declaration) is not _AvatarCleanupDeclaration:
        raise ValueError("avatar_cleanup_invalid")
    registered = _cleanup_declarations.pop(declaration, None)
    if registered is None:
        raise ValueError("avatar_cleanup_invalid")
    tracker, observation = registered
    state = _state(tracker())
    if (
        state.invocation is not observation.invocation
        or _normal_storage.get(tracker()) is not observation
        or not observation.declared
    ):
        raise ValueError("avatar_cleanup_invalid")
    return observation.binding


def _consume_avatar_cleanup_io(permit):
    from core.casdoor.avatar_cleanup_provider import _native_avatar_domain

    if type(permit) is not _AvatarCleanupPermit:
        raise ValueError("avatar_cleanup_invalid")
    registered = _cleanup_permits.pop(permit, None)
    if registered is None:
        raise ValueError("avatar_cleanup_invalid")
    tracker, observation, record = registered
    state = _state(tracker())
    if (
        state.invocation is not observation.invocation
        or _native_avatar_domain() is not observation.domain
        or record.reservation != observation.binding.reservation
    ):
        raise ValueError("avatar_cleanup_invalid")
    return observation.domain, record


def _observe_avatar_cleanup_deleted(domain, record):
    """Only original private FileService owner can seal successful native absence."""
    import inspect
    from core.casdoor.avatar_cleanup_provider import _native_avatar_domain

    frame = inspect.currentframe()
    try:
        if frame.f_back.f_code is not _CLEANUP_FILE_OWNER.__code__ or _native_avatar_domain() is not domain:
            raise ValueError("avatar_cleanup_invalid")
    finally:
        del frame
    result = object.__new__(_AvatarCleanupObservation)
    _cleanup_observations[result] = record
    return result


def _consume_avatar_cleanup_observation(observation):
    if type(observation) is not _AvatarCleanupObservation:
        raise ValueError("avatar_cleanup_invalid")
    record = _cleanup_observations.pop(observation, None)
    if record is None:
        raise ValueError("avatar_cleanup_invalid")
    return record


def _run_avatar_cleanup(tracker, consumer, budget):
    """Actual original closed roots precede one-use I/O; no SQL-derived permit issuer."""
    from core.casdoor.avatar_cleanup_provider import _native_avatar_domain
    from services.casdoor_avatar_consumer_service_extend import CasdoorAvatarConsumerService, _AVATAR_ROOT_OWNER

    observation = _normal_storage.get(tracker) if tracker is not None else None
    if observation is None:
        return False
    state = _state(tracker)
    attached = observation.attached
    if (
        type(consumer) is not CasdoorAvatarConsumerService
        or observation.consumer is not consumer
        or consumer._root.__func__ is not _AVATAR_ROOT_OWNER
        or attached is None
        or not attached.failed
        or attached.commit_attempted
        or attached.committed
        or not attached.clean
        or state.invocation.signal is not None
        or observation.declared
        or _native_avatar_domain() is not observation.domain
        or FileService._cleanup_reserved_avatar is not _CLEANUP_FILE_OWNER
    ):
        return False
    terminal_known = False
    try:
        budget.check()
        observation.declared = True
        declaration = object.__new__(_AvatarCleanupDeclaration)
        _cleanup_declarations[declaration] = (ref(tracker), observation)
        terminal = consumer._root(
            state.invocation, lambda repo: repo._prepare_avatar_cleanup(declaration, now=budget.check()), write=True
        )
        if not (
            terminal.committed
            and terminal.commit_attempted
            and terminal.clean
            and not terminal.failed
            and state.invocation.signal is None
        ):
            return False
        record = terminal.value
        reread = consumer._root(
            state.invocation, lambda repo: repo._read_avatar_cleanup_closed(record, now=budget.check())
        )
        if reread.failed or not reread.clean or reread.value is not True or state.invocation.signal is not None:
            return False
        terminal_known = True
        budget.check()
        if _native_avatar_domain() is not observation.domain:
            return True
        permit = object.__new__(_AvatarCleanupPermit)
        _cleanup_permits[permit] = (ref(tracker), observation, record)
        deleted = FileService._cleanup_reserved_avatar(permit)
        if deleted is not None and state.invocation.signal is None:
            consumer._root(
                state.invocation, lambda repo: repo._complete_avatar_cleanup(deleted, now=budget.check()), write=True
            )
    except BaseException as error:
        state.invocation.latch(error)
    return terminal_known
