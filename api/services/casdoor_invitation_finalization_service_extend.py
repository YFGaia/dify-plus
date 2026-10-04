"""Finalize an already committed invitation operation; return SQL facts only.

The authenticated internal caller owns verified claims, eligibility and complete
leases. No production caller, admission, login/session or remote role effect is
provided. At most one consume API invocation occurs; the unchanged P1 wrapper
may independently retry transport. Every SQL/commit uncertainty aborts.
"""

from dataclasses import dataclass
from math import floor
from time import monotonic

from repositories.casdoor_invitation_finalization_repository_extend import (
    CasdoorInvitationFinalizationRepository,
    InvitationFinalizationFacts,
    local_policy,
)
from repositories.casdoor_invitation_operation_repository_extend import (
    _RECONCILE_WINDOW,
    _SNAPSHOT_FIELDS,
    InvitationOperationAttempt,
    InvitationOperationConflict,
    _attempt,
    _now,
    _require,
    _strict_json,
)
from services.account_adapters import RedisInvitationTokenStore
from services.entities.account_activation_entities import (
    InvitationConsumptionRecovery,
    InvitationRecoveryBinding,
    VersionedInvitationObservation,
)


@dataclass(frozen=True, slots=True, repr=False)
class CommittedInvitationFinalization:
    """Acknowledged SQL completion; never admission or required-resource proof."""

    facts: InvitationFinalizationFacts


class CasdoorInvitationFinalizationService:
    def __init__(self, *, session_factory, store: RedisInvitationTokenStore):
        self._session_factory = session_factory
        self._store = store

    def _read(self, attempt, *, previous=None):
        with self._session_factory() as session, session.begin():
            _require(session is not previous)
            return CasdoorInvitationFinalizationRepository(session).inspect(attempt), session

    @staticmethod
    def _remaining(facts, deadline):
        local_policy()
        _require(monotonic() < deadline)
        seconds = (facts.snapshot.created_at + _RECONCILE_WINDOW - _now()).total_seconds()
        _require(seconds > 0)
        return min(604800, floor(seconds))

    def _recover(self, attempt, facts, *, token, deadline, previous):
        # Fresh SQL facts, then close the Session before P3J's read-only I/O.
        fresh, _session = self._read(attempt, previous=previous)
        _require(fresh == facts and not fresh.completed)
        self._remaining(facts, deadline)
        data = _strict_json(facts.snapshot.desired_json, _SNAPSHOT_FIELDS)
        binding = InvitationRecoveryBinding(
            data["namespace_id"], data["identity_id"], data["account_id"], data["workspace_id"]
        )
        recovery = InvitationConsumptionRecovery(
            binding.namespace_id,
            binding.identity_id,
            binding.account_id,
            binding.workspace_id,
            facts.issuance.payload_json,
            facts.issuance.payload_digest,
            str(facts.snapshot.operation_id),
            data["expected_receipt_json"].encode(),
            deadline,
        )
        result = self._store.recover_invitation_consumption(recovery, token=token, trusted_attempt=binding)
        self._remaining(facts, deadline)
        _require(result.status == "confirmed")

    def _finalize(self, attempt, *, token):
        _attempt(attempt)
        local_policy()
        _require(type(self._store) is RedisInvitationTokenStore)
        facts, initial_session = self._read(attempt)
        if facts.completed:
            confirmed, _reader = self._read(attempt, previous=initial_session)
            _require(confirmed == facts)
            return CommittedInvitationFinalization(confirmed)
        _require(type(token) is str)
        start = monotonic()
        deadline = start + 30.0
        self._remaining(facts, deadline)
        result = self._store.observe_versioned_invitation(token)
        self._remaining(facts, deadline)
        data = _strict_json(facts.snapshot.desired_json, _SNAPSHOT_FIELDS)
        operation_id = str(facts.snapshot.operation_id)
        if result.status == "observed":
            observation = result.observation
            _require(type(observation) is VersionedInvitationObservation)
            _require(type(observation.ttl_ms) is int and observation.ttl_ms > 0)
            deadline = min(deadline, start + observation.ttl_ms / 1000)
            self._remaining(facts, deadline)
            _require(observation._raw == facts.issuance.payload_json.encode())
            _require(observation.payload_digest == facts.issuance.payload_digest)
            _require(
                (observation.payload.account_id, observation.payload.workspace_id)
                == (str(attempt.account_id), str(attempt.workspace_id))
            )
            _keys, receipt = self._store._consumption_arguments(observation, operation_id)
            _require(receipt == data["expected_receipt_json"].encode())
            self._remaining(facts, deadline)
            readback = self._store.read_invitation_consumption(observation, operation_id=operation_id)
            self._remaining(facts, deadline)
            if readback.status == "not_confirmed":
                ttl = self._remaining(facts, deadline)
                _require(ttl >= 1)
                consumed = self._store.consume_versioned_invitation(
                    observation,
                    operation_id=operation_id,
                    receipt_ttl_seconds=ttl,
                )
                self._remaining(facts, deadline)
                if consumed.status == "unknown":
                    self._recover(attempt, facts, token=token, deadline=deadline, previous=initial_session)
                else:
                    _require(consumed.status in ("consumed", "replayed"))
            else:
                _require(readback.status == "confirmed")
        else:
            _require(result.status in ("absent_or_expired", "unavailable", "unknown"))
            self._recover(attempt, facts, token=token, deadline=deadline, previous=initial_session)
        self._remaining(facts, deadline)
        with self._session_factory() as session, session.begin():
            _require(session is not initial_session)
            completed = CasdoorInvitationFinalizationRepository(session).finalize(
                attempt,
                expected=facts,
                receipt_json=data["expected_receipt_json"],
            )
            self._remaining(facts, deadline)
        # An exception during commit is never interpreted as success.
        self._remaining(facts, deadline)
        confirmed, _reader = self._read(attempt, previous=session)
        _require(confirmed == completed and confirmed.completed)
        self._remaining(facts, deadline)
        return CommittedInvitationFinalization(confirmed)

    def finalize(self, attempt: InvitationOperationAttempt, *, token: str | None = None):
        """Authenticated internal use only; an opaque failure permits no admission."""
        signal = None
        try:
            return self._finalize(attempt, token=token)
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except Exception:
            pass
        del attempt, token
        if signal == "interrupt":
            raise KeyboardInterrupt("invitation finalization interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        raise InvitationOperationConflict() from None
