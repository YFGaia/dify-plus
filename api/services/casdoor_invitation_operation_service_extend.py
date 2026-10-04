"""Prepare one durable invitation operation, never consume or finalize it.

The caller has independently verified claims, invitation eligibility, active
configuration, complete leases and the exact candidate. The injected factory is
owned here: first inspect committed SQL, obtain one fresh read-only P1 observation
outside SQL for a new operation, then compose account persistence and the intent
in one root. Only an acknowledged commit plus an exact fresh-session reread
returns a handle. Every owner/SQL/commit uncertainty aborts; no continuation from
a failed savepoint, external action, login permission or production caller exists.
"""

import json
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from repositories.casdoor_invitation_operation_repository_extend import (
    CasdoorInvitationOperationRepository,
    InvitationOperationAttempt,
    InvitationOperationConflict,
    InvitationOperationSnapshot,
    _require,
)
from services.account_adapters import RedisInvitationTokenStore
from services.casdoor_invited_login_account_service_extend import (
    CasdoorInvitedLoginAccountService,
    _PreparedInvitedLoginAccount,
)
from services.entities.account_activation_entities import VersionedInvitationObservation


@dataclass(frozen=True, slots=True, repr=False)
class CommittedInvitationOperation:
    """Acknowledged pending SQL facts, never consumption or admission proof."""

    snapshot: InvitationOperationSnapshot


class CasdoorInvitationOperationService:
    def __init__(
        self, *, session_factory, store: RedisInvitationTokenStore, invited_login: CasdoorInvitedLoginAccountService
    ):
        self._session_factory = session_factory
        self._store = store
        self._invited_login = invited_login

    def _read(self, attempt, *, previous=None):
        with self._session_factory() as session, session.begin():
            _require(session is not previous)
            result, facts = CasdoorInvitationOperationRepository(session).inspect(attempt)
            return result, facts, session

    def _produce(self, attempt, *, token, prepared):
        _require(type(self._store) is RedisInvitationTokenStore)
        _require(type(self._invited_login) is CasdoorInvitedLoginAccountService)
        _require(self._invited_login._activation._tokens is self._store)
        existing, facts, initial_session = self._read(attempt)
        if existing is not None:
            # Exact authenticated operation retry is SQL-only, even after token
            # disappearance. This proves no Redis consumption or finalization.
            confirmed, current, _reader = self._read(attempt, previous=initial_session)
            _require(confirmed == existing and current == facts)
            return CommittedInvitationOperation(existing)
        _require(type(prepared) is _PreparedInvitedLoginAccount and prepared._owner is self._invited_login)
        start = monotonic()
        result = self._store.observe_versioned_invitation(token)
        observation = result.observation
        _require(result.status == "observed" and type(observation) is VersionedInvitationObservation)
        _require(type(observation.ttl_ms) is int and observation.ttl_ms > 0)
        # Start before observation I/O: response transit cannot extend freshness.
        deadline = min(start + 30.0, start + observation.ttl_ms / 1000)
        _require(monotonic() < deadline)
        _require(observation._raw == facts.payload_json.encode() and observation.payload_digest == facts.payload_digest)
        self._store._consumption_arguments(observation, str(uuid4()))
        shared_invitation = prepared._shared.observation.invitation
        _require(
            (
                shared_invitation.account_id,
                shared_invitation.workspace_id,
                shared_invitation.account_email,
                shared_invitation.role,
                shared_invitation.requires_setup,
            )
            == (
                observation.payload.account_id,
                observation.payload.workspace_id,
                observation.payload.email,
                observation.payload.role,
                observation.payload.requires_setup,
            )
        )
        # No network operation follows this point, including on failure/unknown commit.
        with self._session_factory() as session, session.begin():
            _require(session is not initial_session)
            repository = CasdoorInvitationOperationRepository(session)
            raced, current = repository.inspect(attempt)
            _require(current == facts and monotonic() < deadline)
            if raced is not None:
                pending = raced
            else:
                # The preceding inspection acquired all existing helper-owned
                # rows NOWAIT; every helper error exits this entire root.
                account = self._invited_login.persist_invited_login_account(
                    prepared,
                    session=session,
                    context=attempt.context,
                    key=attempt.key,
                )
                _require((account.account_id, account.workspace_id) == (attempt.account_id, attempt.workspace_id))
                pending = repository.create_pending(
                    attempt,
                    store=self._store,
                    observation=observation,
                    observation_deadline=deadline,
                    expected_facts=facts,
                )
                _require(str(account.identity_id) == json.loads(pending.desired_json)["identity_id"])
            _require(monotonic() < deadline)
        # The context's commit must have returned successfully before this runs.
        _require(monotonic() < deadline)
        confirmed, current, _reader = self._read(attempt, previous=session)
        _require(confirmed == pending and current == facts and monotonic() < deadline)
        return CommittedInvitationOperation(pending)

    def produce(self, attempt: InvitationOperationAttempt, *, token: str | None = None, prepared=None):
        """Return only a verified committed operation; IDs are never request fields.

        prepared is the original invited-login owner's same-attempt capability.
        A SQL-only exact retry does not consume it and need not provide a token.
        Any failure requires a newly prepared attempt; no automatic retry occurs.
        """
        signal = None
        try:
            return self._produce(attempt, token=token, prepared=prepared)
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except Exception:
            pass
        del token, prepared, attempt
        if signal == "interrupt":
            raise KeyboardInterrupt("invitation operation interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        raise InvitationOperationConflict() from None
