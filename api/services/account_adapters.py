"""Infrastructure adapters shared by account application services."""

import json
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from time import monotonic
from typing import Any, override
from uuid import UUID
from weakref import WeakKeyDictionary

from pydantic import TypeAdapter, ValidationError
from redis.crc import key_slot

from extensions.ext_redis import RedisClientWrapper
from extensions.redis_names import serialize_redis_name
from libs.helper import RateLimiter, TokenManager
from libs.workspace_permission import check_workspace_member_invite_permission
from services.account_activation_service import (
    AccountActivationEligibility,
    InvitationTokenStore,
    WorkspaceInvitePolicy,
    WorkspaceMemberAccessSync,
    WorkspaceMembershipCache,
)
from services.account_change_email_ports import (
    AccountEmailPolicyGateway,
    ChangeEmailCodeGenerator,
    ChangeEmailNotificationGateway,
    ChangeEmailSecurityGateway,
    ChangeEmailSendLimiter,
    ChangeEmailTokenGateway,
)
from services.account_deletion_feedback_service import AccountDeletionFeedbackGateway
from services.account_education_service import AccountEducationGateway
from services.account_errors import AccountDeletionRateLimitError
from services.account_ports import (
    AccountDeletionScheduler,
    AccountDeletionSyncGateway,
    AccountDeletionVerificationGateway,
    AccountDeletionVerificationNotifier,
)
from services.account_security_gateway import RedisAccountEmailSecurityGateway
from services.billing_service import BillingService
from services.enterprise.account_deletion_sync import sync_account_deletion_memberships
from services.entities.account_activation_entities import (
    InvitationAuthority,
    InvitationConsumptionReadback,
    InvitationConsumptionRecovery,
    InvitationConsumptionResult,
    InvitationLookup,
    InvitationObservationResult,
    InvitationRecoveryBinding,
    InvitationToken,
    VersionedInvitationObservation,
    VersionedInvitationPayload,
)
from services.entities.account_entities import (
    AccountChangeEmailNewEmailToken,
    AccountChangeEmailNewEmailVerifiedToken,
    AccountChangeEmailOldEmailToken,
    AccountChangeEmailOldEmailVerifiedToken,
    AccountChangeEmailPhase,
    AccountChangeEmailTokenData,
    AccountDeletionChallenge,
    AccountEducationActivation,
    AccountEducationAutocomplete,
    AccountEducationStatus,
    AccountEducationVerification,
)
from services.entities.auth_entities import (
    ChangeEmailNewEmailToken,
    ChangeEmailNewEmailVerifiedToken,
    ChangeEmailOldEmailToken,
    ChangeEmailOldEmailVerifiedToken,
    ChangeEmailTokenData,
)
from services.invitation_issuance_service_extend import _reviewed_connection, _reviewed_pool
from tasks.delete_account_task import delete_account_task
from tasks.mail_account_deletion_task import send_account_deletion_verification_code
from tasks.mail_change_mail_task import send_change_mail_completed_notification_task, send_change_mail_task

_invitation_token_adapter = TypeAdapter(InvitationToken)
_change_email_token_adapter: TypeAdapter[ChangeEmailTokenData] = TypeAdapter(ChangeEmailTokenData)

_CHANGE_EMAIL_RATE_LIMIT_ATTEMPTS = 1
_CHANGE_EMAIL_RATE_LIMIT_SECONDS = 60
_ACCOUNT_DELETION_RATE_LIMIT_ATTEMPTS = 1
_ACCOUNT_DELETION_RATE_LIMIT_SECONDS = 60


# EVAL receives already serialized physical byte keys. No legacy scalar key is used.
_INVITATION_OBSERVE = """
local kind = redis.call('TYPE', KEYS[1]).ok
if kind == 'none' then return {'absent_or_expired'} end
if kind ~= 'string' then return {'invalid'} end
local ttl = redis.call('PTTL', KEYS[1])
if ttl == -1 then return {'invalid'} end
if ttl <= 0 then return {'absent_or_expired'} end
if redis.call('STRLEN', KEYS[1]) > 8192 then return {'invalid'} end
return {'observed', redis.call('GET', KEYS[1]), ttl}
"""
_INVITATION_CONSUME = """
local receipt_type = redis.call('TYPE', KEYS[2]).ok
local token_type = redis.call('TYPE', KEYS[1]).ok
if receipt_type ~= 'none' then
    if receipt_type == 'string' and redis.call('PTTL', KEYS[2]) > 0
        and redis.call('GET', KEYS[2]) == ARGV[2] and token_type == 'none' then
        return 'replayed'
    end
    return 'receipt_conflict'
end
if token_type ~= 'string' then return 'unknown' end
if redis.call('PTTL', KEYS[1]) <= 0 then return 'unknown' end
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 'mismatch' end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3], 'NX')
redis.call('DEL', KEYS[1])
return 'consumed'
"""
_INVITATION_READBACK = """
local receipt_type = redis.call('TYPE', KEYS[2]).ok
local token_type = redis.call('TYPE', KEYS[1]).ok
if receipt_type ~= 'none' then
    if receipt_type == 'string' and redis.call('PTTL', KEYS[2]) > 0
        and redis.call('GET', KEYS[2]) == ARGV[2] and token_type == 'none' then
        return 'confirmed'
    end
    return 'unknown'
end
if token_type == 'string' and redis.call('PTTL', KEYS[1]) > 0
    and redis.call('GET', KEYS[1]) == ARGV[1] then return 'not_confirmed' end
return 'unknown'
"""


class _InvitationStorageUncertain(Exception):
    """Fixed private-storage failure; never contains Redis exception details."""


_INVITATION_RECOVERY_MAX_SECONDS = 30.0


def _invitation_recovery_remaining(deadline: float) -> float:
    if type(deadline) not in (int, float) or not isfinite(deadline):
        raise _InvitationStorageUncertain("invitation_storage_uncertain")
    remaining = deadline - monotonic()
    if not 0 < remaining <= _INVITATION_RECOVERY_MAX_SECONDS:
        raise _InvitationStorageUncertain("invitation_storage_uncertain")
    return remaining


def _invitation_uuid(value: object, *, token: bool = False) -> bool:
    if type(value) is not str or len(value) != 36:
        return False
    try:
        parsed = UUID(value)
        return str(parsed) == value and (not token or parsed.version == 4)
    except ValueError:
        return False


def _invitation_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invitation_constant(value: str) -> None:
    raise ValueError


def _invitation_depth(value: Any, depth: int = 1) -> None:
    if depth > 4:
        raise ValueError
    if type(value) is dict:
        for child in value.values():
            _invitation_depth(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _invitation_depth(child, depth + 1)


def _parse_versioned_invitation(raw: bytes, token: str) -> tuple[str, VersionedInvitationPayload | None]:
    """Strict v1 parser, deliberately independent of the permissive legacy DTOs."""
    from libs.helper import email as validate_email

    try:
        if type(raw) is not bytes or len(raw) > 8192:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_invitation_pairs, parse_constant=_invitation_constant)
        _invitation_depth(data)
        legacy = {"account_id", "email", "workspace_id", "role", "requires_setup"}
        if type(data) is not dict or set(data) not in (legacy, legacy | {"invitation_authority"}):
            raise ValueError
        if not _invitation_uuid(data["account_id"]) or not _invitation_uuid(data["workspace_id"]):
            raise ValueError
        for name in ("email", "role"):
            value = data[name]
            if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 255:
                raise ValueError
        validate_email(data["email"])
        if type(data["requires_setup"]) is not bool:
            raise ValueError
        if "invitation_authority" not in data:
            return "capability_stale", None
        authority = data["invitation_authority"]
        if type(authority) is not dict or type(authority.get("schema_version")) is not int:
            raise ValueError
        if authority["schema_version"] != 1:
            return "unsupported_version", None
        if set(authority) != {
            "schema_version",
            "issuance_id",
            "lifecycle_id",
            "lifecycle_epoch",
            "token_digest",
            "join_id_at_issue",
        }:
            raise ValueError
        if not all(_invitation_uuid(authority[name]) for name in ("issuance_id", "lifecycle_id")):
            raise ValueError
        if authority["join_id_at_issue"] is not None and not _invitation_uuid(authority["join_id_at_issue"]):
            raise ValueError
        if type(authority["lifecycle_epoch"]) is not int or not 1 <= authority["lifecycle_epoch"] <= 2**53 - 1:
            raise ValueError
        if (
            type(authority["token_digest"]) is not str
            or authority["token_digest"] != sha256(token.encode()).hexdigest()
        ):
            raise ValueError
        return "observed", VersionedInvitationPayload(
            account_id=data["account_id"],
            email=data["email"],
            workspace_id=data["workspace_id"],
            role=data["role"],
            requires_setup=data["requires_setup"],
            invitation_authority=InvitationAuthority(**authority),
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return "invalid", None


class RedisInvitationTokenStore(InvitationTokenStore):
    def __init__(self, *, redis: RedisClientWrapper) -> None:
        self._redis = redis
        self._observations: WeakKeyDictionary[VersionedInvitationObservation, tuple[Any, ...]] = WeakKeyDictionary()

    def _strong_eval(self, script: str, keys: tuple[bytes, ...], *args: bytes | int) -> Any:
        signal = None
        try:
            from libs.sensitive_redis import sensitive_redis_call

            with sensitive_redis_call():
                result = self._redis.eval(script, len(keys), *keys, *args)
            return result
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except BaseException:
            signal = "error"
        # Leave both context and handler before raising, clearing raw exception chains.
        del script, keys, args
        result = None
        if signal == "interrupt":
            raise KeyboardInterrupt("sensitive Redis call interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        raise _InvitationStorageUncertain("invitation_storage_uncertain") from None

    @staticmethod
    def _observation_binding(observation: VersionedInvitationObservation) -> tuple[Any, ...]:
        payload = observation.payload
        authority = payload.invitation_authority
        values = (
            payload.account_id,
            payload.email,
            payload.workspace_id,
            payload.role,
            payload.requires_setup,
            authority.schema_version,
            authority.issuance_id,
            authority.lifecycle_id,
            authority.lifecycle_epoch,
            authority.token_digest,
            authority.join_id_at_issue,
            observation.ttl_ms,
            observation.payload_digest,
            observation.key_digest,
            observation._raw,
            observation._physical_key,
            observation._prefix,
        )
        return tuple((type(value), value) for value in values)

    def observe_versioned_invitation(self, token: str) -> InvitationObservationResult:
        if not _invitation_uuid(token, token=True):
            return InvitationObservationResult("invalid")
        try:
            prefix = self._redis._get_prefix()
            if type(prefix) is not str:
                return InvitationObservationResult("unavailable")
            key = serialize_redis_name(self._invitation_token_key(token), prefix).encode("utf-8")
            response = self._strong_eval(_INVITATION_OBSERVE, (key,))
        except Exception:
            return InvitationObservationResult("unavailable")
        if type(response) is not list or not response:
            return InvitationObservationResult("unknown")
        if response in ([b"absent_or_expired"], [b"invalid"]):
            return InvitationObservationResult("invalid" if response[0] == b"invalid" else "absent_or_expired")
        if len(response) != 3 or response[0] != b"observed":
            return InvitationObservationResult("unknown")
        raw, ttl = response[1:]
        if type(ttl) is not int or ttl <= 0:
            return InvitationObservationResult("invalid")
        status, payload = _parse_versioned_invitation(raw, token)
        if payload is None:
            return InvitationObservationResult(status)
        observation = VersionedInvitationObservation(
            payload, ttl, sha256(raw).hexdigest(), sha256(key).hexdigest(), raw, key, prefix
        )
        self._observations[observation] = self._observation_binding(observation)
        return InvitationObservationResult("observed", observation)

    def _consumption_arguments(
        self, observation: VersionedInvitationObservation, operation_id: str
    ) -> tuple[tuple[bytes, bytes], bytes]:
        if (
            type(observation) is not VersionedInvitationObservation
            or self._observations.get(observation) != self._observation_binding(observation)
            or not _invitation_uuid(operation_id)
            or self._redis._get_prefix() != observation._prefix
            or sha256(observation._raw).hexdigest() != observation.payload_digest
            or sha256(observation._physical_key).hexdigest() != observation.key_digest
        ):
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        key = observation._physical_key
        base = serialize_redis_name(
            f"member_invite:receipt:{observation.key_digest}:{operation_id}:", observation._prefix
        ).encode("utf-8")
        slot = key_slot(key)
        receipt_key = next(
            (candidate for i in range(65536) if key_slot(candidate := base + i.to_bytes(2, "big")) == slot), None
        )
        if receipt_key is None or key == receipt_key or key_slot(receipt_key) != key_slot(key):
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        payload = observation.payload
        authority = payload.invitation_authority
        receipt = {
            "schema_version": 1,
            "status": "consumed",
            "operation_id": operation_id,
            "issuance_id": authority.issuance_id,
            "lifecycle_id": authority.lifecycle_id,
            "lifecycle_epoch": authority.lifecycle_epoch,
            "account_id": payload.account_id,
            "workspace_id": payload.workspace_id,
            "join_id_at_issue": authority.join_id_at_issue,
            "token_digest": authority.token_digest,
            "payload_digest": observation.payload_digest,
            "key_digest": observation.key_digest,
        }
        return (key, receipt_key), json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def consume_versioned_invitation(
        self, observation: VersionedInvitationObservation, *, operation_id: str, receipt_ttl_seconds: int
    ) -> InvitationConsumptionResult:
        if type(receipt_ttl_seconds) is not int or not 1 <= receipt_ttl_seconds <= 604800:
            return InvitationConsumptionResult("unavailable")
        try:
            keys, receipt = self._consumption_arguments(observation, operation_id)
        except Exception:
            return InvitationConsumptionResult("unavailable")
        try:
            result = self._strong_eval(_INVITATION_CONSUME, keys, observation._raw, receipt, receipt_ttl_seconds)
        except Exception:
            return InvitationConsumptionResult("unknown")
        statuses = {
            b"consumed": "consumed",
            b"replayed": "replayed",
            b"mismatch": "mismatch",
            b"receipt_conflict": "receipt_conflict",
        }
        return InvitationConsumptionResult(statuses.get(result, "unknown") if type(result) is bytes else "unknown")

    def read_invitation_consumption(
        self, observation: VersionedInvitationObservation, *, operation_id: str
    ) -> InvitationConsumptionReadback:
        try:
            keys, receipt = self._consumption_arguments(observation, operation_id)
        except Exception:
            return InvitationConsumptionReadback("unavailable")
        try:
            result = self._strong_eval(_INVITATION_READBACK, keys, observation._raw, receipt)
        except Exception:
            return InvitationConsumptionReadback("unknown")
        statuses = {b"confirmed": "confirmed", b"not_confirmed": "not_confirmed"}
        return InvitationConsumptionReadback(statuses.get(result, "unknown") if type(result) is bytes else "unknown")

    def _recovery_arguments(
        self,
        recovery: InvitationConsumptionRecovery,
        token: str,
        trusted_attempt: InvitationRecoveryBinding,
    ) -> tuple[tuple[bytes, bytes], bytes, bytes]:
        # These facts are supplied by the trusted durable-operation caller, not
        # reconstructed from request claims. No observation is minted/registered.
        if (
            type(recovery) is not InvitationConsumptionRecovery
            or type(trusted_attempt) is not InvitationRecoveryBinding
        ):
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        _invitation_recovery_remaining(recovery.deadline_monotonic)
        for name in ("namespace_id", "identity_id", "account_id", "workspace_id"):
            value = getattr(recovery, name)
            if not _invitation_uuid(value) or value != getattr(trusted_attempt, name):
                raise _InvitationStorageUncertain("invitation_storage_uncertain")
        if (
            not _invitation_uuid(token, token=True)
            or not _invitation_uuid(recovery.operation_id, token=True)
            or type(recovery.payload_json) is not str
            or type(recovery.payload_digest) is not str
            or type(recovery.expected_receipt) is not bytes
        ):
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        raw = recovery.payload_json.encode("utf-8")
        if sha256(raw).hexdigest() != recovery.payload_digest:
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        status, payload = _parse_versioned_invitation(raw, token)
        if (
            status != "observed"
            or payload is None
            or payload.account_id != recovery.account_id
            or payload.workspace_id != recovery.workspace_id
        ):
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        prefix = self._redis._get_prefix()
        if type(prefix) is not str:
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        key = serialize_redis_name(self._invitation_token_key(token), prefix).encode("utf-8")
        key_digest = sha256(key).hexdigest()
        authority = payload.invitation_authority
        receipt = {
            "schema_version": 1,
            "status": "consumed",
            "operation_id": recovery.operation_id,
            "issuance_id": authority.issuance_id,
            "lifecycle_id": authority.lifecycle_id,
            "lifecycle_epoch": authority.lifecycle_epoch,
            "account_id": payload.account_id,
            "workspace_id": payload.workspace_id,
            "join_id_at_issue": authority.join_id_at_issue,
            "token_digest": authority.token_digest,
            "payload_digest": recovery.payload_digest,
            "key_digest": key_digest,
        }
        expected = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        # Byte equality also rejects duplicate/extra keys and noncanonical receipt
        # encodings. Prefix drift changes key_digest and is rejected before I/O.
        if expected != recovery.expected_receipt:
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        base = serialize_redis_name(f"member_invite:receipt:{key_digest}:{recovery.operation_id}:", prefix).encode(
            "utf-8"
        )
        slot = key_slot(key)
        receipt_key = next(
            (candidate for i in range(65536) if key_slot(candidate := base + i.to_bytes(2, "big")) == slot), None
        )
        if receipt_key is None or key == receipt_key or key_slot(receipt_key) != slot:
            raise _InvitationStorageUncertain("invitation_storage_uncertain")
        _invitation_recovery_remaining(recovery.deadline_monotonic)
        return (key, receipt_key), raw, expected

    def _read_recovery_once(
        self, keys: tuple[bytes, bytes], raw: bytes, receipt: bytes, deadline: float
    ) -> InvitationConsumptionReadback:
        connection = None
        signal = None
        completed = False
        clean = False
        try:
            from libs.sensitive_redis import sensitive_redis_call

            with sensitive_redis_call():
                _invitation_recovery_remaining(deadline)
                pool, cluster = _reviewed_pool(self._redis._require_client(), keys[0])
                try:
                    connection = pool.get_connection()
                    _reviewed_connection(connection, pool, cluster)
                    remaining = _invitation_recovery_remaining(deadline)
                    old_timeout = connection._sock.gettimeout()
                    timeout = min(old_timeout, remaining) if old_timeout is not None and old_timeout > 0 else remaining
                    connection._sock.settimeout(timeout)
                    packed = connection.pack_command("EVAL", _INVITATION_READBACK, 2, *keys, raw, receipt)
                    _invitation_recovery_remaining(deadline)
                    connection.send_packed_command(packed, check_health=False)
                    remaining = _invitation_recovery_remaining(deadline)
                    connection._sock.settimeout(min(timeout, remaining))
                    result = connection.read_response(disable_decoding=True, disconnect_on_error=True)
                    _invitation_recovery_remaining(deadline)
                    completed = True
                except KeyboardInterrupt:
                    signal = "interrupt"
                    raise
                except SystemExit:
                    signal = "exit"
                    raise
                finally:
                    if connection is not None:
                        try:
                            connection.disconnect()
                            if connection._sock is not None:
                                raise _InvitationStorageUncertain("invitation_storage_uncertain")
                        except BaseException:
                            connection.mark_for_reconnect()
                            raise
                        finally:
                            pool.release(connection)
                    clean = True
            if signal is None:
                return InvitationConsumptionReadback(
                    "confirmed"
                    if completed and clean and type(result) is bytes and result == b"confirmed"
                    else "unknown"
                )
        except KeyboardInterrupt:
            signal = signal or "interrupt"
        except SystemExit:
            signal = signal or "exit"
        except Exception:
            pass
        del keys, raw, receipt, connection
        if signal == "interrupt":
            raise KeyboardInterrupt("invitation recovery interrupted") from None
        if signal == "exit":
            raise SystemExit(1) from None
        return InvitationConsumptionReadback("unknown")

    def recover_invitation_consumption(
        self,
        recovery: InvitationConsumptionRecovery,
        *,
        token: str,
        trusted_attempt: InvitationRecoveryBinding,
    ) -> InvitationConsumptionReadback:
        """Read-only fresh-process recovery; never admits or consumes membership."""
        signal = None
        try:
            keys, raw, receipt = self._recovery_arguments(recovery, token, trusted_attempt)
        except KeyboardInterrupt:
            signal = "interrupt"
        except SystemExit:
            signal = "exit"
        except Exception:
            return InvitationConsumptionReadback("unavailable")
        if signal is not None:
            del recovery, token, trusted_attempt
            if signal == "interrupt":
                raise KeyboardInterrupt("invitation recovery interrupted") from None
            raise SystemExit(1) from None
        return self._read_recovery_once(keys, raw, receipt, recovery.deadline_monotonic)

    @override
    def find(self, invitation: InvitationLookup) -> InvitationToken | None:
        if invitation.workspace_id is not None and invitation.email is not None:
            account_id = self._redis.get(self._workspace_invitation_key(invitation))
            if account_id is None:
                return None
            return InvitationToken(
                account_id=account_id.decode("utf-8"),
                email=invitation.email,
                workspace_id=invitation.workspace_id,
            )

        data = self._redis.get(self._invitation_token_key(invitation.token))
        if data is None:
            return None
        return _invitation_token_adapter.validate_json(data)

    @override
    def revoke(self, invitation: InvitationLookup) -> None:
        if invitation.workspace_id is not None and invitation.email is not None:
            self._redis.delete(self._workspace_invitation_key(invitation))
        else:
            self._redis.delete(self._invitation_token_key(invitation.token))

    @staticmethod
    def _invitation_token_key(token: str) -> str:
        return f"member_invite:token:{token}"

    @staticmethod
    def _workspace_invitation_key(invitation: InvitationLookup) -> str:
        assert invitation.workspace_id is not None
        assert invitation.email is not None
        email_hash = sha256(invitation.email.encode()).hexdigest()
        return f"member_invite_token:{invitation.workspace_id}, {email_hash}:{invitation.token}"


class DeploymentWorkspaceInvitePolicy(WorkspaceInvitePolicy):
    @override
    def ensure_allowed(self, workspace_id: str) -> None:
        check_workspace_member_invite_permission(workspace_id)


class BillingAccountActivationEligibility(AccountActivationEligibility):
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    @override
    def get_freeze_type(self, email: str) -> str | None:
        if not self._enabled:
            return None
        return BillingService.get_email_freeze_type(email)


class BillingWorkspaceMembershipCache(WorkspaceMembershipCache):
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    @override
    def invalidate(self, workspace_id: str) -> None:
        if self._enabled:
            BillingService.clean_billing_info_cache(workspace_id)


class RBACWorkspaceMemberAccessSync(WorkspaceMemberAccessSync):
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    @override
    def sync(self, workspace_id: str, account_id: str) -> None:
        if not self._enabled:
            return

        from tasks.initialize_created_app_rbac_access_task import sync_joined_workspace_member_rbac_access_task

        sync_joined_workspace_member_rbac_access_task.delay(
            workspace_id,
            account_id,
            operator_account_id=None,
        )


class BillingAccountEducationGateway(AccountEducationGateway):
    @override
    def verify(self, *, account_id: str) -> AccountEducationVerification:
        result = BillingService.EducationIdentity.verify(account_id=account_id) or {}
        return AccountEducationVerification(token=result.get("token"))

    @override
    def activate(
        self,
        *,
        account_id: str,
        tenant_id: str,
        token: str,
        institution: str,
        role: str,
    ) -> AccountEducationActivation:
        result = BillingService.EducationIdentity.activate(
            account_id=account_id,
            tenant_id=tenant_id,
            token=token,
            institution=institution,
            role=role,
        )
        return AccountEducationActivation(message=result["message"])

    @override
    def status(self, account_id: str) -> AccountEducationStatus:
        result = BillingService.EducationIdentity.status(account_id) or {}
        expire_at = result.get("expire_at")
        return AccountEducationStatus(
            result=result.get("result"),
            is_student=result.get("is_student"),
            expire_at=datetime.fromisoformat(expire_at).astimezone(UTC) if isinstance(expire_at, str) else expire_at,
            allow_refresh=result.get("allow_refresh"),
        )

    @override
    def autocomplete(self, *, keywords: str, page: int, limit: int) -> AccountEducationAutocomplete:
        result = BillingService.EducationIdentity.autocomplete(keywords, page, limit) or {}
        return AccountEducationAutocomplete(
            data=tuple(result.get("data") or ()),
            curr_page=result.get("curr_page"),
            has_next=result.get("has_next"),
        )


class BillingAccountDeletionFeedbackGateway(AccountDeletionFeedbackGateway):
    @override
    def submit(self, *, email: str, feedback: str) -> None:
        BillingService.update_account_deletion_feedback(email, feedback)


class TokenManagerChangeEmailTokenGateway(ChangeEmailTokenGateway):
    @override
    def get(self, token: str) -> AccountChangeEmailTokenData | None:
        payload = TokenManager.get_token_data(token, "change_email")
        if payload is None:
            return None
        try:
            token_data = _change_email_token_adapter.validate_python(payload)
        except ValidationError:
            return None
        token_kwargs = {
            "account_id": token_data.account_id,
            "email": token_data.email,
            "old_email": token_data.old_email,
            "code": token_data.code,
        }
        if isinstance(token_data, ChangeEmailOldEmailToken):
            return AccountChangeEmailOldEmailToken(**token_kwargs)
        if isinstance(token_data, ChangeEmailOldEmailVerifiedToken):
            return AccountChangeEmailOldEmailVerifiedToken(**token_kwargs)
        if isinstance(token_data, ChangeEmailNewEmailToken):
            return AccountChangeEmailNewEmailToken(**token_kwargs)
        if isinstance(token_data, ChangeEmailNewEmailVerifiedToken):
            return AccountChangeEmailNewEmailVerifiedToken(**token_kwargs)
        return None

    @override
    def issue(self, token_data: AccountChangeEmailTokenData) -> str:
        return TokenManager.generate_token(
            account_id=token_data.account_id,
            email=token_data.email,
            token_type="change_email",
            additional_data={
                "old_email": token_data.old_email,
                "code": token_data.code,
                "email_change_phase": token_data.phase.value,
            },
        )

    @override
    def revoke(self, token: str) -> None:
        TokenManager.revoke_token(token, "change_email")


class SecureChangeEmailCodeGenerator(ChangeEmailCodeGenerator):
    @override
    def generate(self) -> str:
        return "".join(str(secrets.randbelow(exclusive_upper_bound=10)) for _ in range(6))


class CeleryChangeEmailNotificationGateway(ChangeEmailNotificationGateway):
    @override
    def send_code(self, *, email: str, code: str, language: str, phase: AccountChangeEmailPhase) -> None:
        send_change_mail_task.delay(language=language, to=email, code=code, phase=phase)

    @override
    def send_completed(self, *, email: str, language: str) -> None:
        send_change_mail_completed_notification_task.delay(language=language, to=email)


class RateLimiterChangeEmailSendLimiter(ChangeEmailSendLimiter):
    def __init__(
        self,
        *,
        redis: RedisClientWrapper | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if rate_limiter is None:
            if redis is None:
                raise ValueError("redis is required when rate_limiter is not provided")
            rate_limiter = RateLimiter(
                prefix="change_email_rate_limit",
                max_attempts=_CHANGE_EMAIL_RATE_LIMIT_ATTEMPTS,
                time_window=_CHANGE_EMAIL_RATE_LIMIT_SECONDS,
                redis_client=redis,
            )
        self._rate_limiter = rate_limiter

    @override
    def is_limited(self, email: str) -> bool:
        return self._rate_limiter.is_rate_limited(email)

    @override
    def record(self, email: str) -> None:
        self._rate_limiter.increment_rate_limit(email)

    @property
    @override
    def retry_after_minutes(self) -> int:
        return int(self._rate_limiter.time_window / 60)


class RedisChangeEmailSecurityGateway(RedisAccountEmailSecurityGateway, ChangeEmailSecurityGateway):
    def __init__(
        self,
        *,
        redis: RedisClientWrapper,
        email_send_ip_limit_per_minute: int,
        verification_failure_limit: int,
        verification_lockout_duration: int,
    ) -> None:
        super().__init__(
            redis=redis,
            email_send_ip_limit_per_minute=email_send_ip_limit_per_minute,
            verification_failure_limit=verification_failure_limit,
            verification_lockout_duration=verification_lockout_duration,
            verification_key_prefix="change_email_error_rate_limit",
        )


class BillingAccountEmailPolicyGateway(AccountEmailPolicyGateway):
    def __init__(self, *, billing_enabled: bool) -> None:
        self._billing_enabled = billing_enabled

    @override
    def is_frozen(self, email: str) -> str | None:
        if not self._billing_enabled or not BillingService.is_email_in_freeze(email):
            return None
        return BillingService.get_email_freeze_type(email) or "freeze"


class TokenManagerAccountDeletionVerificationGateway(AccountDeletionVerificationGateway):
    @override
    def create(self, *, account_id: str, email: str) -> AccountDeletionChallenge:
        code = "".join(str(secrets.randbelow(exclusive_upper_bound=10)) for _ in range(6))
        token = TokenManager.generate_token(
            account_id=account_id,
            email=email,
            token_type="account_deletion",
            additional_data={"code": code},
        )
        return AccountDeletionChallenge(token=token, code=code)

    @override
    def verify(self, *, account_id: str, token: str, code: str) -> bool:
        token_data = TokenManager.get_token_data(token, "account_deletion")
        if token_data is None:
            return False
        return token_data.get("account_id") == account_id and token_data.get("code") == code


class CeleryAccountDeletionVerificationNotifier(AccountDeletionVerificationNotifier):
    def __init__(
        self,
        *,
        redis: RedisClientWrapper | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if rate_limiter is None:
            if redis is None:
                raise ValueError("redis is required when rate_limiter is not provided")
            rate_limiter = RateLimiter(
                prefix="email_code_account_deletion_rate_limit",
                max_attempts=_ACCOUNT_DELETION_RATE_LIMIT_ATTEMPTS,
                time_window=_ACCOUNT_DELETION_RATE_LIMIT_SECONDS,
                redis_client=redis,
            )
        self._rate_limiter = rate_limiter

    @override
    def send(self, *, email: str, code: str) -> None:
        if self._rate_limiter.is_rate_limited(email):
            raise AccountDeletionRateLimitError(int(self._rate_limiter.time_window / 60))

        send_account_deletion_verification_code.delay(to=email, code=code)
        self._rate_limiter.increment_rate_limit(email)


class EnterpriseAccountDeletionSyncGateway(AccountDeletionSyncGateway):
    @override
    def sync(self, *, account_id: str, workspace_ids: Sequence[str]) -> bool:
        return sync_account_deletion_memberships(
            account_id=account_id,
            workspace_ids=workspace_ids,
            source="account_deleted",
        )


class CeleryAccountDeletionScheduler(AccountDeletionScheduler):
    @override
    def schedule(self, account_id: str) -> None:
        delete_account_task.delay(account_id)
