"""Initial-only avatar consumer; construction/dispatch and durable recovery remain held.

Each repository call owns a fresh explicit SQL root. Only actual owner outputs
from this invocation permit the next stage. Synchronous codec/storage cannot be
preempted; observing a late completion retains the reservation for manual recovery.
"""

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from core.casdoor.avatar_termination import (
    _enter_avatar_storage,
    _fetch_before_storage,
    _normalize_before_storage,
    _register_committed_avatar_claim,
    _seal_pre_storage,
    _start_pre_storage,
)
from core.casdoor.crypto import EncryptionContext, EncryptionPurpose
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from sqlalchemy.orm import Session, sessionmaker

from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from services.file_service import FileService


@dataclass(frozen=True)
class _InitialResult:
    code: Literal["applied", "unknown"]
    file_id: UUID | None = None


class _Stop(Exception):
    """Internal, fixed reason only; never carries provider exceptions."""


class _Invocation:
    def __init__(self):
        self.signal = None

    def latch(self, error):
        if self.signal is None:
            if isinstance(error, KeyboardInterrupt):
                self.signal = "interrupt"
            elif isinstance(error, SystemExit):
                self.signal = "exit"


@dataclass(repr=False)
class _RootResult:
    value: object = None
    crypto: object = None
    committed: bool = False
    commit_attempted: bool = False
    clean: bool = True
    failed: bool = False
    reason: str = "commit_unknown"


class _Budget:
    def __init__(self, now, monotonic, source, lease):
        self.now = now
        self.monotonic = monotonic
        self.source = source
        self.lease = lease
        self.ceiling = None
        self.check()

    def check(self, lease=None):
        # Monotonic precedes wall time, so sampling latency cannot enlarge budget.
        mono = self.monotonic()
        utc = _utc(self.now())
        if type(mono) not in (int, float) or not math.isfinite(mono):
            raise _Stop("lease_expired")
        if lease is not None:
            self.lease = min(self.lease, lease)
        remaining = min(15.0, (self.source - utc).total_seconds(), (self.lease - utc).total_seconds())
        proposed = mono + remaining
        if remaining <= 0 or not math.isfinite(proposed):
            raise _Stop("lease_expired")
        self.ceiling = proposed if self.ceiling is None else min(self.ceiling, proposed)
        if mono >= self.ceiling:
            raise _Stop("lease_expired")
        return utc


def _utc(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _Stop("commit_unknown")
    return value


def _supplier(crypto, source, budget):
    """Closure contains ciphertext and AAD only; plaintext exists inside B0 scopes."""

    def decrypt():
        budget.check()
        plaintext = crypto.decrypt(
            source.ciphertext,
            context=EncryptionContext(
                EncryptionPurpose.AVATAR_URL, source.namespace_id, source.revision_id, record_id=str(source.intent_id)
            ),
        )
        if (
            type(plaintext) is not str
            or not 1 <= len(plaintext.encode("utf-8")) <= 4096
            or hashlib.sha256(plaintext.encode("utf-8")).hexdigest() != source.sha256
        ):
            raise ValueError("avatar_source_invalid")
        return plaintext

    return decrypt


class CasdoorAvatarConsumerService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        configuration_service: CasdoorConfigurationService,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
    ):
        self._session_factory = session_factory
        self._configuration_service = configuration_service
        self._now = now
        self._monotonic = monotonic

    def _root(self, invocation, operation, *, write=False):
        """Independently rollback AND close even when either cleanup step fails."""
        result = _RootResult()
        session = repository = configuration = None
        try:
            session = self._session_factory()
            session.begin()
            configuration = self._configuration_service._repository(session)
            repository = CasdoorAvatarRepository(session, configuration_repository=configuration)
            result.value = operation(repository)
            result.crypto = configuration.crypto
            if write:
                result.commit_attempted = True
                session.commit()
                result.committed = True
        except BaseException as error:
            invocation.latch(error)
            result.failed = True
            if isinstance(error, _Stop):
                result.reason = str(error)
        finally:
            if session is not None:
                try:
                    # This also ends a failed commit's still-active caller root.
                    session.rollback()
                except BaseException as error:
                    invocation.latch(error)
                    result.clean = False
                try:
                    session.close()
                except BaseException as error:
                    invocation.latch(error)
                    result.clean = False
            session = repository = configuration = None
        _register_committed_avatar_claim(invocation, result)
        return result

    @staticmethod
    def _read(result):
        if result.failed or not result.clean:
            raise _Stop(result.reason)
        return result.value

    def _consume_initial(self, intent_id: UUID) -> _InitialResult:
        invocation = _Invocation()
        outcome = _InitialResult("unknown")
        attempt = claim = authority = fetched = normalized = crypto = supplier = budget = None
        reason = "commit_unknown"
        termination = None
        try:
            if type(intent_id) is not UUID:
                raise _Stop("commit_unknown")
            replay = self._read(
                self._root(invocation, lambda repo: repo.reconcile_intent_attachment(intent_id, now=_utc(self._now())))
            )
            if replay.code == "applied":
                outcome = _InitialResult("applied", replay.result_file_id)
            elif replay.code == "not_applied":
                claimed = self._root(
                    invocation, lambda repo: repo.claim_and_reserve(intent_id, now=_utc(self._now())), write=True
                )
                # Only acknowledged commit grants this invocation a known claim.
                claim = claimed.value
                if claimed.committed and claim.code == "reserved":
                    attempt = claim.attempt
                self._read(claimed)
                if claim.code == "replayed":
                    replay = self._read(
                        self._root(
                            invocation, lambda repo: repo.reconcile_intent_attachment(intent_id, now=_utc(self._now()))
                        )
                    )
                    if replay.code == "applied":
                        outcome = _InitialResult("applied", replay.result_file_id)
                elif claim.code == "reserved":
                    reason = "attachment_lost"
                    checked = self._root(
                        invocation, lambda repo: repo.recheck_attempt_for_io(attempt, now=_utc(self._now()))
                    )
                    if checked.failed:
                        raise _Stop(checked.reason if checked.reason != "commit_unknown" else reason)
                    authority = self._read(checked)
                    if (authority.attempt, authority.source, authority.reservation) != (
                        attempt,
                        claim.source,
                        claim.reservation,
                    ):
                        raise _Stop(reason)
                    crypto = checked.crypto
                    reason = "lease_expired"
                    budget = _Budget(
                        self._now, self._monotonic, authority.source.expires_at, authority.lease_expires_at
                    )
                    termination = _start_pre_storage(invocation, claimed, authority)
                    supplier = _supplier(crypto, authority.source, budget)
                    budget.check()
                    reason = "fetch_unknown"
                    fetched = _fetch_before_storage(termination, supplier, budget)
                    supplier = crypto = None
                    if (fetched.status, fetched.reason, fetched.termination) != ("ok", "ok", "confirmed"):
                        raise _Stop(
                            {
                                "rejected": "fetch_rejected",
                                "failed": "fetch_failed",
                                "cancelled": "fetch_cancelled",
                            }.get(fetched.status, "fetch_unknown")
                        )
                    reason = "image_rejected"
                    budget.check()
                    image = _normalize_before_storage(termination, fetched, budget)
                    fetched = None
                    if image.code != "normalized" or image.image is None:
                        raise _Stop(reason)
                    normalized = image.image
                    image = None
                    reason = "attachment_lost"
                    checked = self._root(
                        invocation, lambda repo: repo.recheck_attempt_for_io(attempt, now=budget.check())
                    )
                    if checked.failed:
                        raise _Stop(checked.reason if checked.reason != "commit_unknown" else reason)
                    fresh = self._read(checked)
                    if (fresh.attempt, fresh.source, fresh.reservation) != (
                        authority.attempt,
                        authority.source,
                        authority.reservation,
                    ):
                        raise _Stop(reason)
                    budget.check(fresh.lease_expires_at)
                    reason = "storage_unknown"
                    _enter_avatar_storage(termination)
                    stored = FileService.store_reserved_avatar(authority.reservation, normalized)
                    budget.check()
                    if stored != "stored":
                        raise _Stop(reason)
                    reason = "attachment_lost"
                    attached = self._root(
                        invocation,
                        lambda repo: repo.recheck_and_attach(attempt, normalized, now=budget.check()),
                        write=True,
                    )
                    if attached.commit_attempted:
                        # Never trust attach's return, including an acknowledged commit.
                        reason = "commit_unknown"
                        reconciled = self._root(
                            invocation,
                            lambda repo: repo.reconcile_attachment(
                                attempt,
                                now=_utc(self._now()),
                                expected_sha3_256=normalized.sha3_256,
                                expected_size=len(normalized.content),
                            ),
                        )
                        proof = self._read(reconciled)
                        if attached.clean and proof.code == "applied":
                            outcome = _InitialResult("applied", proof.result_file_id)
                    if outcome.code != "applied":
                        raise _Stop(reason)
        except _Stop as error:
            reason = str(error)
        except BaseException as error:
            invocation.latch(error)
        # Finish in a separate root, outside original handlers and private scopes.
        confirmed = False
        if attempt is not None and outcome.code != "applied" and invocation.signal is None:
            capability = _seal_pre_storage(termination)
            if capability is not None:
                terminal = self._root(
                    invocation,
                    lambda repo: repo.confirm_pre_storage_failure(attempt, capability, now=budget.check()),
                    write=True,
                )
                if terminal.commit_attempted:
                    # Even a lost commit ACK gets a fresh exact reader; it is never
                    # reissued, retried or promoted to a public successful outcome.
                    reread = self._root(
                        invocation,
                        lambda repo: repo.reconcile_pre_storage_failure(terminal.value, now=_utc(self._now())),
                    )
                    confirmed = (
                        terminal.committed
                        and terminal.clean
                        and not terminal.failed
                        and reread.clean
                        and not reread.failed
                        and reread.value is True
                    )
        if attempt is not None and outcome.code != "applied" and not confirmed:
            self._root(
                invocation, lambda repo: repo.finish_attempt(attempt, reason=reason, now=_utc(self._now())), write=True
            )
        termination = None
        attempt = claim = authority = fetched = normalized = crypto = supplier = budget = None
        if invocation.signal == "interrupt":
            raise KeyboardInterrupt("avatar consumption interrupted")
        if invocation.signal == "exit":
            raise SystemExit(1)
        return outcome
