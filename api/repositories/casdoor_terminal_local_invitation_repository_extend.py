"""Bounded historical LOCAL invitation closure; SQL facts confer no authority.

This non-locking candidate reader accepts only an authenticated caller's exact
context/key/account and complete current scalar scope. It neither exempts an
intent nor supplies a writer guard. A future ordinary root owner must acquire
parents in the documented hierarchy and reread these facts in its own root.
Historical D/F hashes are strictly parsed and cross-linked, never compared to
today's roles/history or used as current permission evidence. Trusted SQL is
assumed; coordinated database forgery is outside this observation's guarantee.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID
from weakref import WeakSet

import sqlalchemy as sa
from core.casdoor.invited_finalization_receipt import RECEIPT_KIND as F_ACTION
from core.casdoor.invited_finalization_receipt import InvitedLocalFinalizationReceipt
from core.casdoor.invited_write_receipt import RECEIPT_KIND as D_ACTION
from core.casdoor.invited_write_receipt import InvitedLocalWriteReceipt
from models.account import AccountStatus, TenantAccountRole
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from sqlalchemy.exc import SQLAlchemyError

from repositories.casdoor_account_preflight_repository_extend import _validate
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from repositories.casdoor_invitation_finalization_repository_extend import _PROOF_KIND, _proof, local_policy
from repositories.casdoor_invitation_operation_repository_extend import (
    _RECEIPT_FIELDS,
    _RECONCILE_WINDOW,
    _SNAPSHOT_FIELDS,
    InvitationOperationSnapshot,
    _canonical,
    _deadline,
    _digest,
    _now,
    _strict_json,
    _uuid,
    operation_idempotency_key,
)
from repositories.casdoor_invited_write_receipt_repository_extend import (
    CasdoorInvitedWriteReceiptRepository,
    _time,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
    LoginScope,
)
from repositories.invitation_authority_repository_extend import MAX_LIFECYCLE_EPOCH, _payload, _receipt


def _require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


@dataclass(frozen=True, slots=True, repr=False)
class TerminalLocalInvitationObservation:
    """Frozen facts only; never a capability or a postwrite equality target.

    Historical rows remain immutable. Current generation/lifecycle/Join rows
    can change through the original ordinary owners. A future guard must reprove
    closure and apply those owners' complete controlled-effect checks, rather
    than require this whole observation to equal a prewrite observation.
    """

    operation_id: UUID
    current_generation: int
    result_epoch: int
    write_summary_sha256: str
    finalization_summary_sha256: str
    _historical_rows: tuple
    _current_rows: tuple


class CasdoorTerminalLocalInvitationRepository:
    def __init__(self, session, configuration_factory):
        self.session = session
        self.configuration_factory = configuration_factory
        self._reader = CasdoorInvitedWriteReceiptRepository(session, configuration_factory)
        self._projection = CasdoorLoginScopeRepository(session, configuration_factory)

    def _current(self, context, key, account_id, scope):
        _validate(context, key, account_id, None)
        _require(type(account_id) is UUID and type(scope) is LoginScope)
        current = self._projection._project_scope(context, key, account_id, creation_email=None, extra_workspace_ids=())
        _require(current == scope)
        _require(
            current.account is not None
            and current.account.id == str(account_id)
            and current.account.status is AccountStatus.ACTIVE
            and current.account.initialized_at is not None
            and _time(current.account.initialized_at) <= _now()
            and len(current.identities) == 1
        )
        identity = current.identities[0]
        _require(
            (
                identity.namespace_id,
                identity.account_id,
                identity.issuer,
                identity.organization,
                identity.subject,
                identity.subject_digest,
            )
            == (str(key.namespace_id), str(account_id), key.issuer, key.organization, key.subject, key.subject_digest)
        )
        return current

    def _rows(self, intent):
        e = self._reader
        operation = e._one(Intent, Intent.id == intent.id)
        data = _strict_json(operation.desired_json, _SNAPSHOT_FIELDS)
        # The caller supplies no issuance/lifecycle selector. Parse the exact
        # discovered operation first, then bound each linked historical row.
        for field in ("issuance_id", "lifecycle_id", "revision_id", "workspace_id"):
            _uuid(data[field])
        _require(data["workspace_id"] == intent.workspace_id)
        lifecycle = e._one(
            Lifecycle, Lifecycle.account_id == intent.account_id, Lifecycle.workspace_id == intent.workspace_id
        )
        issuance = e._one(Issuance, Issuance.issuance_id == data["issuance_id"], text_limit=8192)
        joins = e._bounded_rows(Join, Join.account_id == intent.account_id, Join.tenant_id == intent.workspace_id)
        revision = e._one(Revision, Revision.id == data["revision_id"])
        namespace = e._one(Namespace, Namespace.id == intent.namespace_id)
        duplicate_operations = e._bounded_rows(
            Intent,
            sa.or_(Intent.id == intent.id, Intent.idempotency_key == operation.idempotency_key),
        )
        _require(duplicate_operations == (operation,))
        d = e._one(Audit, Audit.action == D_ACTION, Audit.correlation_id == intent.id, text_limit=32768)
        f = e._one(Audit, Audit.action == F_ACTION, Audit.correlation_id == intent.id, text_limit=32768)
        return operation, lifecycle, issuance, joins, revision, d, f, namespace

    def _bounded_orm(self, model, row):
        """Feed the original owner only rows admitted by the same byte WHERE.

        Keep a strong reference until its Session.get calls finish. Otherwise
        SQLAlchemy's weak identity map could issue an unbounded replacement.
        """
        table = model.__table__
        texts = tuple(
            c
            for c in table.columns
            if isinstance(c.type, sa.Text) or isinstance(getattr(c.type, "impl", None), sa.Text)
        )
        lengths = tuple(
            sa.func.length(sa.cast(c, sa.LargeBinary))
            if self.session.get_bind().dialect.name == "sqlite"
            else sa.func.octet_length(c)
            for c in texts
        )
        entity = self.session.scalar(
            sa.select(model)
            .where(
                *(c == getattr(row, c.name) for c in table.primary_key.columns),
                *(sa.or_(c.is_(None), length <= 16384) for c, length in zip(texts, lengths, strict=True)),
            )
            .execution_options(populate_existing=True)
        )
        _require(entity is not None and all(getattr(entity, c.name) == getattr(row, c.name) for c in table.columns))
        return entity

    def _verify(self, context, key, account_id, scope, rows):
        operation, lifecycle, issuance, _joins, historical_revision, d_audit, f_audit, namespace = rows
        data = _strict_json(operation.desired_json, _SNAPSHOT_FIELDS)
        consumed = _strict_json(data["expected_receipt_json"], _RECEIPT_FIELDS)
        _receipt(data["expected_receipt_json"])
        payload = _payload(issuance.payload_json)
        authority = payload["invitation_authority"]
        _uuid(operation.id, version4=True)
        _digest(consumed["key_digest"])
        _require(sha256(issuance.payload_json.encode()).hexdigest() == issuance.payload_digest)
        _require(
            (issuance.account_id, issuance.workspace_id, issuance.email, issuance.role, issuance.requires_setup)
            == (str(account_id), operation.workspace_id, payload["email"], payload["role"], payload["requires_setup"])
        )
        _require((payload["account_id"], payload["workspace_id"]) == (issuance.account_id, issuance.workspace_id))
        _require(
            (
                issuance.issuance_id,
                issuance.lifecycle_id,
                issuance.lifecycle_epoch,
                issuance.token_digest,
                issuance.join_id_at_issue,
            )
            == (
                authority["issuance_id"],
                authority["lifecycle_id"],
                authority["lifecycle_epoch"],
                authority["token_digest"],
                authority["join_id_at_issue"],
            )
        )
        expected_consumed = {
            "schema_version": 1,
            "status": "consumed",
            "operation_id": operation.id,
            "issuance_id": issuance.issuance_id,
            "lifecycle_id": issuance.lifecycle_id,
            "lifecycle_epoch": issuance.lifecycle_epoch,
            "account_id": issuance.account_id,
            "workspace_id": issuance.workspace_id,
            "join_id_at_issue": issuance.join_id_at_issue,
            "token_digest": issuance.token_digest,
            "payload_digest": issuance.payload_digest,
            "key_digest": consumed["key_digest"],
        }
        _require(data["expected_receipt_json"] == _canonical(expected_consumed))
        _require(issuance.state == "consumed" and issuance.consumption_receipt_json == data["expected_receipt_json"])
        _receipt(issuance.consumption_receipt_json)
        if issuance.actor_id is not None:
            _uuid(issuance.actor_id)
        identity = scope.identities[0]
        _require(type(data["generation"]) is int and 0 <= data["generation"] < 2**63 - 1)
        _require(type(data["fence_epoch"]) is int and 0 <= data["fence_epoch"] <= context.fence_epoch)
        # Reconstruct the old revision through its original digest/namespace
        # owner. It need not be today's active revision; no secret is decrypted.
        owner = self.configuration_factory(self.session)
        _require(type(owner) is CasdoorConfigurationRepository and owner.session is self.session)
        loaded = (self._bounded_orm(Revision, historical_revision), self._bounded_orm(Namespace, namespace))
        revision = owner._revision(str(context.integration_id), data["revision_id"])
        _require(
            (historical_revision.integration_id, historical_revision.namespace_id, historical_revision.config_digest)
            == (str(context.integration_id), str(context.namespace_id), data["config_digest"])
            and revision.id == historical_revision.id
            and revision is loaded[0]
        )
        observed_role = data["observed_join_role"]
        _require(
            observed_role is None
            or type(observed_role) is str
            and observed_role in {r.value for r in TenantAccountRole}
        )
        _require((observed_role is None) == (issuance.join_id_at_issue is None))
        expected = {
            "schema_version": 1,
            "kind": "invitation_finalize",
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": historical_revision.id,
            "config_digest": historical_revision.config_digest,
            "identity_id": identity.id,
            "subject_digest": key.subject_digest,
            "account_id": str(account_id),
            "workspace_id": issuance.workspace_id,
            "generation": data["generation"],
            "fence_epoch": data["fence_epoch"],
            "issuance_id": issuance.issuance_id,
            "payload_digest": issuance.payload_digest,
            "lifecycle_id": issuance.lifecycle_id,
            "lifecycle_epoch": issuance.lifecycle_epoch,
            "join_id_at_issue": issuance.join_id_at_issue,
            "observed_join_role": observed_role,
            "operation_id": operation.id,
            "expected_receipt_json": data["expected_receipt_json"],
            "reconcile_until_utc": _deadline(_time(operation.created_at)),
        }
        _require(operation.desired_json == _canonical(expected))
        _require(operation.scope_digest == sha256(operation.desired_json.encode()).hexdigest())
        _require(operation.idempotency_key == operation_idempotency_key(UUID(issuance.issuance_id)))
        for field in (
            "namespace_id",
            "identity_id",
            "account_id",
            "workspace_id",
            "revision_id",
            "generation",
            "fence_epoch",
        ):
            _require(getattr(operation, field) == expected[field])
        _require(tuple(scope.intents[0]) == tuple(getattr(operation, k) for k in scope.intents[0]._mapping))
        _require(
            operation.kind is CasdoorIntentKind.INVITATION_FINALIZE
            and operation.operation_state is CasdoorOperationState.APPLIED
            and operation.termination_state is CasdoorTerminationState.CONFIRMED
            and operation.termination_proof_kind == _PROOF_KIND
            and operation.membership_id is None
            and operation.ownership_epoch == 0
            and operation.attempt_count == 0
        )
        _require(
            all(
                getattr(operation, field) is None
                for field in (
                    "resource_type",
                    "resource_id",
                    "attempt_id",
                    "lease_owner",
                    "lease_expires_at",
                    "sent_at",
                    "acknowledged_at",
                    "readback_at",
                    "retry_at",
                    "error_code",
                )
            )
        )
        _require(type(operation.proof_ref) is str and operation.proof_ref.isascii() and len(operation.proof_ref) <= 128)
        match = re.fullmatch(r"v1:([0-9a-f-]{36}):([01]):([1-9][0-9]{0,15}):([0-9a-f]{64})", operation.proof_ref)
        _require(match is not None)
        join_id, created_text, epoch_text, _ = match.groups()
        _uuid(join_id)
        created, result_epoch = created_text == "1", int(epoch_text)
        _require(result_epoch <= MAX_LIFECYCLE_EPOCH and result_epoch == issuance.lifecycle_epoch + int(created))
        _require(created == (issuance.join_id_at_issue is None))
        old_role = issuance.role if created else observed_role
        _require(type(old_role) is str and old_role in {r.value for r in TenantAccountRole})
        _require(
            (created and old_role != TenantAccountRole.OWNER) or (not created and join_id == issuance.join_id_at_issue)
        )
        _require(
            _time(operation.created_at) <= _time(operation.terminated_at) < operation.created_at + _RECONCILE_WINDOW
            and operation.terminated_at == _time(operation.updated_at)
            and operation.terminated_at <= _now()
            and _time(issuance.created_at)
            <= operation.created_at
            <= _time(issuance.consumed_at)
            <= operation.terminated_at
        )
        _require(_time(issuance.consumed_at) <= _time(issuance.updated_at) <= _now())
        _uuid(lifecycle.lifecycle_id, version4=True)
        _require(
            (lifecycle.lifecycle_id, lifecycle.account_id, lifecycle.workspace_id)
            == (issuance.lifecycle_id, str(account_id), issuance.workspace_id)
            and lifecycle.state in ("active", "withdrawn")
            and type(lifecycle.epoch) is int
            and result_epoch <= lifecycle.epoch <= MAX_LIFECYCLE_EPOCH
            and _time(lifecycle.created_at) <= _time(lifecycle.updated_at) <= _now()
            and lifecycle.created_at <= issuance.created_at
            and identity.sync_generation >= data["generation"] + 1
        )
        snapshot = InvitationOperationSnapshot(
            UUID(operation.id),
            operation.desired_json,
            operation.scope_digest,
            operation.idempotency_key,
            operation.created_at,
        )
        _require(
            operation.proof_ref
            == _proof(snapshot, data, join_id=join_id, join_role=old_role, created=created, result_epoch=result_epoch)
        )
        d_receipt, f_receipt = (
            InvitedLocalWriteReceipt(d_audit.summary_json),
            InvitedLocalFinalizationReceipt(f_audit.summary_json),
        )
        d, f = d_receipt.values(), f_receipt.values()
        refs = {
            "operation_id": operation.id,
            "issuance_id": issuance.issuance_id,
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": historical_revision.id,
            "identity_id": identity.id,
            "account_id": str(account_id),
            "workspace_id": issuance.workspace_id,
            "invitation_join_id": join_id,
        }
        _require(d["references"] == refs == f["references"])
        _require(
            (
                d["generation_before"],
                d["generation_after"],
                d["fence_epoch"],
                d["scope_digest"],
                d["completion_proof_ref"],
                d["payload_digest"],
            )
            == (
                data["generation"],
                data["generation"] + 1,
                data["fence_epoch"],
                operation.scope_digest,
                operation.proof_ref,
                issuance.payload_digest,
            )
        )
        _require((f["generation"], f["fence_epoch"]) == (d["generation_after"], d["fence_epoch"]))
        d_hash = sha256(d_receipt.canonical_json.encode("utf-8")).hexdigest()
        _require(f["write_receipt_sha256"] == d_hash and f["before_postwrite_sha256"] == d["postwrite_sha256"])
        from repositories.casdoor_terminal_retained_invitation_facts_extend import terminal_facts

        retained_rows = terminal_facts(
            self._reader, d_audit, d, f_audit, f, operation, owner._configuration(revision)
        )
        if retained_rows is None:
            created_ids = [r["membership_id"] for r in d["results"] if r["membership_created"]]
            _require(all(type(item) is str for item in created_ids) and len(created_ids) == len(set(created_ids)))
            _require(f["finalized_ids"] == sorted(created_ids))
            for item in d["results"]:
                if item["membership_created"]:
                    _require(
                        item["ownership_decision"] == "new_join_required"
                        and item["outcome"] == "applied"
                        and not item["role_changed"]
                        and item["metadata_changed"]
                    )
                else:
                    _require(
                        item["membership_id"] is None
                        and item["outcome"] == "preserved"
                        and not item["role_changed"]
                        and not item["metadata_changed"]
                        and item["ownership_decision"]
                        == ("owner_protected" if item["current_role"] == "owner" else "preserve_unmanaged")
                    )
        for audit, action in ((d_audit, D_ACTION), (f_audit, F_ACTION)):
            _uuid(audit.id)
            _require(
                audit.action == action
                and audit.correlation_id == operation.id
                and audit.actor_account_id is None
                and audit.result_code == "verified"
            )
            _require(
                all(
                    getattr(audit, field) == refs[field]
                    for field in ("namespace_id", "revision_id", "identity_id", "account_id")
                )
            )
        _require(operation.terminated_at <= _time(d_audit.created_at) <= _time(f_audit.created_at) <= _now())
        return TerminalLocalInvitationObservation(
            UUID(operation.id),
            identity.sync_generation,
            result_epoch,
            d_hash,
            sha256(f_receipt.canonical_json.encode("utf-8")).hexdigest(),
            (
                operation,
                issuance,
                tuple(
                    (name, value) for name, value in historical_revision._mapping.items() if name != "encrypted_secret"
                ),
                d_audit,
                f_audit,
                retained_rows,
            ),
            (lifecycle, _joins, namespace),
        )

    def observe(self, context, key, account_id, *, scope):
        """Observe zero or one precise terminal intent; no lock or exemption.

        The complete scope must be freshly derived by the original owner. All
        mixed, extra, unknown or malformed non-avatar intents remain pending.
        Root binding for a writer and authentication remain caller obligations.
        """
        try:
            root = self._reader._root()
            local_policy()
            with self.session.no_autoflush:
                current = self._current(context, key, account_id, scope)
                if not current.intents:
                    self._reader._root(expected=root)
                    return None
                _require(len(current.intents) == 1)
                intent = current.intents[0]
                _require(intent.kind is CasdoorIntentKind.INVITATION_FINALIZE)
                _require(intent.workspace_id in {row.id for row in current.workspaces})
                rows = self._rows(intent)
                observation = self._verify(context, key, account_id, current, rows)
                _require(self._current(context, key, account_id, scope) == current)
                reread = self._rows(intent)
                _require(reread == rows and self._verify(context, key, account_id, current, reread) == observation)
                self._reader._root(expected=root)
                return observation
        except (
            SQLAlchemyError,
            ValueError,
            RuntimeError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            StopIteration,
        ):
            raise CasdoorLoginScopeConflict() from None


class _OrdinaryTerminalAttempt:
    """Identity capability issued only inside the actual authenticated C2 stack."""

    def __init__(self, prepared, roles, leases, configuration, scope):
        self.prepared = prepared
        self.roles = roles
        self.leases = leases
        self.configuration = configuration
        self.scope = scope
        self.history = None


class _OrdinaryTerminalRoot:
    """A root is unusable until its complete parent hook has proved closure."""

    def __init__(self, attempt, session, configuration_factory):
        self.attempt = attempt
        self.session = session
        self.transaction = session.get_transaction()
        self.repository = CasdoorTerminalLocalInvitationRepository(session, configuration_factory)
        self.ready = False
        self.operation_id = None
        self.workspace_id = None


_ordinary_attempts: WeakSet = WeakSet()
_ordinary_roots: WeakSet = WeakSet()


def _begin_ordinary_terminal_attempt(*, prepared, roles, leases, configuration):
    # A private DTO factory alone would still mint from forged observations.
    # Bind issuance to the original successful verifier/loader frame itself.
    import sys

    from core.casdoor.admission import AdmissionAction
    from core.casdoor.auth_transactions import AuthMode
    from services.casdoor_local_login_coordinator_service_extend import CasdoorLocalLoginCoordinatorService

    frame = sys._getframe(1)
    values = frame.f_locals
    _require(frame.f_code is CasdoorLocalLoginCoordinatorService._coordinate_local_login.__code__)
    consumed = values["consumed"]
    _require(
        values["prepared"] is prepared
        and values["roles"] is roles
        and values["leases"] is leases
        and values["config"] is configuration
        and prepared.plan.action is AdmissionAction.USE_BOUND
        and consumed.context.mode is AuthMode.LOGIN
        and all(
            value is None
            for value in (
                consumed.context.invite,
                consumed.context.source,
                consumed.context.identity_id,
                consumed.context.action,
            )
        )
        and (values["fresh_profile"], values["fresh_preflight"], values["fresh_plan"], values["final_scope"])
        == (values["profile"], values["preflight"], values["plan"], values["fresh_scope"])
        and values["fresh_online"] == values["online"]
        and prepared.preflight.exact_binding is not None
        and values["final_scope"].configuration == configuration
        and values["final_scope"].lease_scope.canonical_keys == leases.canonical_keys
        and leases._deadline == prepared.deadline
    )
    leases.ensure_owned()
    attempt = _OrdinaryTerminalAttempt(prepared, roles, leases, configuration, values["final_scope"])
    _ordinary_attempts.add(attempt)
    return attempt


def _end_ordinary_terminal_attempt(attempt):
    if attempt is not None:
        _ordinary_attempts.discard(attempt)


def _ordinary_attempt_check(attempt, prepared, roles, leases):
    from configs import dify_config
    from enums import DeploymentEdition
    from services.casdoor_login_account_service_extend import _check_deadline

    _require(
        type(attempt) is _OrdinaryTerminalAttempt
        and attempt in _ordinary_attempts
        and attempt.prepared is prepared
        and attempt.roles is roles
        and attempt.leases is leases
        and dify_config.DEPLOYMENT_EDITION == DeploymentEdition.COMMUNITY
        and dify_config.RBAC_ENABLED is False
        and leases._deadline == prepared.deadline
    )
    _check_deadline(prepared.deadline)
    leases.ensure_owned()
    _check_deadline(prepared.deadline)


def _bind_ordinary_terminal_root(attempt, session, configuration_factory):
    import sys

    from services.casdoor_local_login_finalization_service_extend import CasdoorLocalLoginFinalizationService
    from services.casdoor_local_login_service_extend import CasdoorLocalLoginService

    frame = sys._getframe(1)
    _require(
        frame.f_code
        in (
            CasdoorLocalLoginService._persist_local_login.__code__,
            CasdoorLocalLoginFinalizationService._finalize_local_login.__code__,
        )
    )
    values = frame.f_locals
    _ordinary_attempt_check(attempt, values["prepared"], values["roles"], values["leases"])
    CasdoorLoginScopeRepository(session, configuration_factory)._clean()
    guard = _OrdinaryTerminalRoot(attempt, session, configuration_factory)
    _ordinary_roots.add(guard)
    return guard


def _ordinary_root_check(guard, session, *, ready=True):
    _require(
        type(guard) is _OrdinaryTerminalRoot
        and guard in _ordinary_roots
        and guard.session is session
        and session.get_transaction() is guard.transaction
        and guard.transaction is not None
        and guard.transaction.is_active
        and (guard.ready or not ready)
    )
    attempt = guard.attempt
    _ordinary_attempt_check(attempt, attempt.prepared, attempt.roles, attempt.leases)
    guard.repository._reader._root(expected=guard.transaction)
    return attempt


def _ordinary_terminal_observe(guard):
    attempt = _ordinary_root_check(guard, guard.session, ready=False)
    prepared = attempt.prepared
    scope = guard.repository._projection.discover(prepared)
    _require(
        scope.configuration == attempt.configuration
        and scope.lease_scope.canonical_keys == attempt.leases.canonical_keys
    )
    observation = guard.repository.observe(prepared.plan.context, prepared.key, prepared.account_id, scope=scope)
    _require(observation is not None)
    history = (observation.operation_id, observation.result_epoch, observation._historical_rows)
    if attempt.history is None:
        attempt.history = history
    else:
        _require(history == attempt.history)
    _require(guard.operation_id is None or guard.operation_id == str(observation.operation_id))
    return scope, observation


def _lock_ordinary_terminal_parents(guard, session, context, key, account_id, parents):
    """Fixed scope hook: all workspace parents precede these child locks."""
    attempt = _ordinary_root_check(guard, session, ready=False)
    prepared = attempt.prepared
    _require(
        context == prepared.plan.context
        and key == prepared.key
        and account_id == prepared.account_id
        and tuple(str(row.id) for row in parents)
        == tuple(str(item.workspace_id) for item in attempt.scope.lease_scope.members)
    )
    scope = guard.repository._projection.discover(prepared)
    _require(len(scope.intents) == 1 and scope.intents[0].kind is CasdoorIntentKind.INVITATION_FINALIZE)
    intent = scope.intents[0]
    operation = guard.repository._reader._one(Intent, Intent.id == intent.id)
    data = _strict_json(operation.desired_json, _SNAPSHOT_FIELDS)
    # Only bounded header refs select rows; observe revalidates the locked chain.
    for field in ("issuance_id", "lifecycle_id", "workspace_id"):
        _uuid(data[field])
    _require(data["workspace_id"] in {row.id for row in parents})
    selections = (
        (Lifecycle, (Lifecycle.account_id == str(account_id), Lifecycle.workspace_id == data["workspace_id"])),
        (Issuance, (Issuance.issuance_id == data["issuance_id"],)),
        (Join, (Join.account_id == str(account_id), Join.tenant_id == data["workspace_id"])),
        (Intent, (Intent.id == intent.id,)),
        (Audit, (Audit.action == D_ACTION, Audit.correlation_id == intent.id)),
        (Audit, (Audit.action == F_ACTION, Audit.correlation_id == intent.id)),
    )
    for model, conditions in selections:
        primary = tuple(model.__table__.primary_key.columns)
        rows = tuple(
            session.execute(sa.select(*primary).where(*conditions).order_by(*primary).limit(2).with_for_update())
        )
        _require(len(rows) <= 1 and (model is Join or len(rows) == 1))
    guard.operation_id, guard.workspace_id = intent.id, data["workspace_id"]
    _ordinary_terminal_observe(guard)
    guard.ready = True


def _ordinary_terminal_exclusion(guard, session, account_id, workspace_id=None):
    attempt = _ordinary_root_check(guard, session)
    _require(account_id == attempt.prepared.account_id)
    _ordinary_terminal_observe(guard)
    if workspace_id is not None:
        _require(str(workspace_id) in {str(item.workspace_id) for item in attempt.scope.lease_scope.members})
        if str(workspace_id) != guard.workspace_id:
            return None
    return guard.operation_id


def _ordinary_terminal_last(guard):
    if guard is not None:
        _ordinary_root_check(guard, guard.session)
        _ordinary_terminal_observe(guard)
