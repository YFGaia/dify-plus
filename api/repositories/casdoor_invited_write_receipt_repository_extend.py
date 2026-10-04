"""Current invited-write SQL consistency, with original G and current G+1.

This reader assumes trusted database persistence. The frozen receipt hash binds
only its documented privacy projection, not arbitrary coordinated SQL rewrites.
No old-generation ORM scope, writer guard, intent exemption or admission result
is manufactured. SQLite tests cover sequential observations only, not missing
row serialization, production contention or a cross-service global snapshot.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransactionOrigin

from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MAX_RECEIPT_BYTES,
    MEMBERSHIP_FIELDS,
    RECEIPT_KIND,
    InvitedLocalWriteReceipt,
    postwrite_sha256,
)
from core.casdoor.ownership import MembershipBackend, MembershipObservation, role_baseline_json, roles_fingerprint
from models.account import AccountStatus, TenantAccountRole, TenantStatus
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceLifecycle,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_invitation_finalization_repository_extend import _PROOF_KIND, _proof, local_policy
from repositories.casdoor_invitation_operation_repository_extend import (
    _RECEIPT_FIELDS,
    _RECONCILE_WINDOW,
    _SNAPSHOT_FIELDS,
    InvitationOperationSnapshot,
    _attempt,
    _canonical,
    _deadline,
    _digest,
    _now,
    _strict_json,
    _uuid,
    operation_idempotency_key,
)
from repositories.casdoor_login_scope_repository_extend import (
    MAX_SCOPE_ROWS,
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)
from repositories.invitation_authority_repository_extend import MAX_LIFECYCLE_EPOCH, _payload
from services.account_email import normalize_email


@dataclass(frozen=True, slots=True, repr=False)
class InvitedWriteReceiptObservation:
    """Non-authoritative observation, stale on root close; copying grants nothing."""

    receipt: InvitedLocalWriteReceipt
    operation_id: UUID
    completion_proof_ref: str
    current_generation: int


def _require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


def _time(value):
    _require(type(value) is datetime and value.tzinfo is None)
    return value


def _primitive(row, fields):
    return {
        name: getattr(row, name).value if isinstance(getattr(row, name), StrEnum) else getattr(row, name)
        for name in fields
    }


class CasdoorInvitedWriteReceiptRepository:
    def __init__(self, session, configuration_factory):
        self.session = session
        self.configuration_factory = configuration_factory

    def _root(self, *, first=False, expected=None):
        session = self.session
        _require(isinstance(session, Session))
        root = session.get_transaction()
        _require(
            root is not None
            and root.is_active
            and root.origin is SessionTransactionOrigin.BEGIN
            and session.is_active
            and not session.in_nested_transaction()
            and not session.new
            and not session.dirty
            and not session.deleted
            and (expected is None or root is expected)
        )
        if first:
            # The pinned SQLAlchemy root must not have enlisted any connection.
            connections = getattr(root, "_connections", None)
            _require(type(connections) is dict and not connections)
        return root

    def _bounded_rows(self, model, *conditions, lock=False, cap=1, text_limit=16384):
        """Bound every selected TEXT byte length before full-row materialization."""
        self._root()
        table = model.__table__
        keys = tuple(table.primary_key.columns)
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
        header_query = sa.select(*keys, *lengths).where(*conditions).order_by(*keys).limit(cap + 1)
        if lock:
            header_query = header_query.with_for_update(nowait=True)
        headers = tuple(self.session.execute(header_query))
        _require(len(headers) <= cap)
        for row in headers:
            for size in tuple(row)[len(keys) :]:
                _require(size is None or type(size) is int and 0 <= size <= text_limit)
        query = (
            sa.select(*table.columns)
            .where(
                *conditions,
                *(sa.or_(c.is_(None), length <= text_limit) for c, length in zip(texts, lengths, strict=True)),
            )
            .order_by(*keys)
            .limit(cap + 1)
        )
        if lock:
            query = query.with_for_update(nowait=True)
        rows = tuple(self.session.execute(query))
        _require(len(rows) == len(headers))
        _require(
            tuple(tuple(getattr(row, c.name) for c in keys) for row in rows)
            == tuple(tuple(row)[: len(keys)] for row in headers)
        )
        return rows

    def _one(self, model, *conditions, lock=False, text_limit=16384):
        rows = self._bounded_rows(model, *conditions, lock=lock, text_limit=text_limit)
        _require(len(rows) == 1)
        return rows[0]

    def _scope(self, projection, attempt):
        scope = projection._project_scope(
            attempt.context,
            attempt.key,
            attempt.account_id,
            creation_email=None,
            extra_workspace_ids=(attempt.workspace_id,),
        )
        account = scope.account
        _require(
            account is not None
            and account.id == str(attempt.account_id)
            and account.status is AccountStatus.ACTIVE
            and account.initialized_at is not None
        )
        _require(_time(account.initialized_at) <= _now())
        _require(account.normalized_email in (None, normalize_email(account.email)))
        _require(len(scope.identities) == 1)
        identity = scope.identities[0]
        _require(
            tuple(identity)[1:]
            == (
                str(attempt.key.namespace_id),
                str(attempt.account_id),
                attempt.key.issuer,
                attempt.key.organization,
                attempt.key.subject,
                attempt.key.subject_digest,
                attempt.expected_generation + 1,
            )
        )
        _require(sum(row.lifecycle is CasdoorNamespaceLifecycle.ACTIVE for row in scope.namespaces) == 1)
        invited = tuple(row for row in scope.workspaces if row.id == str(attempt.workspace_id))
        _require(len(invited) == 1 and invited[0].status is TenantStatus.NORMAL)
        # The ordinary barrier stays visible, including its original G. It is
        # never waived for this reader or converted to completed admission.
        _require(len(scope.intents) == 1)
        return scope

    def _rows(self, attempt, *, lock):
        # Parents have already been acquired in the shared hierarchy. No child
        # discovery is used to late-lock a new account/workspace parent.
        aid, wid = str(attempt.account_id), str(attempt.workspace_id)
        lifecycle = self._one(Lifecycle, Lifecycle.account_id == aid, Lifecycle.workspace_id == wid, lock=lock)
        issuance = self._one(Issuance, Issuance.issuance_id == str(attempt.issuance_id), lock=lock, text_limit=8192)
        join = self._one(Join, Join.account_id == aid, Join.tenant_id == wid, lock=lock)
        operation = self._one(
            Intent,
            Intent.idempotency_key == operation_idempotency_key(attempt.issuance_id),
            lock=lock,
        )
        audit = self._one(
            Audit,
            Audit.action == RECEIPT_KIND,
            Audit.correlation_id == operation.id,
            lock=lock,
            text_limit=MAX_RECEIPT_BYTES,
        )
        # These fields are excluded from the frozen digest. Read them with a
        # separate bound for current canonical-role consistency, not historical
        # equality. The complete actual set is compared to the scalar scope.
        join_ids = sa.select(Join.id).where(Join.account_id == aid)
        histories = self._bounded_rows(
            History,
            sa.or_(History.account_id == aid, History.join_id.in_(join_ids)),
            cap=MAX_SCOPE_ROWS,
            text_limit=16384,
        )
        return lifecycle, issuance, join, operation, audit, histories

    def _invitation(self, attempt, scope, rows):
        lifecycle, issuance, join, operation, audit, histories = rows
        context, identity = attempt.context, scope.identities[0]
        payload = _payload(issuance.payload_json)
        authority = payload["invitation_authority"]
        _require(sha256(issuance.payload_json.encode()).hexdigest() == issuance.payload_digest)
        _require(scope.account.email == payload["email"])
        _require(
            (issuance.account_id, issuance.workspace_id, issuance.email, issuance.role, issuance.requires_setup)
            == (
                str(attempt.account_id),
                str(attempt.workspace_id),
                payload["email"],
                payload["role"],
                payload["requires_setup"],
            )
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
        _require(lifecycle.lifecycle_id == issuance.lifecycle_id and lifecycle.state == "active")
        _require(type(lifecycle.epoch) is int and 1 <= lifecycle.epoch <= MAX_LIFECYCLE_EPOCH)
        if issuance.actor_id is not None:
            _uuid(issuance.actor_id)
        _require(_time(lifecycle.created_at) <= _time(lifecycle.updated_at) <= _now())
        _require(_time(issuance.created_at) <= _time(issuance.updated_at) <= _now())
        data = _strict_json(operation.desired_json, _SNAPSHOT_FIELDS)
        consumed = _strict_json(data["expected_receipt_json"], _RECEIPT_FIELDS)
        _uuid(operation.id, version4=True)
        _digest(consumed["key_digest"])
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
        role = join.role.value if type(join.role) is TenantAccountRole else join.role
        _require(type(role) is str and role in {r.value for r in TenantAccountRole})
        # Build the original immutable bytes explicitly at recorded G. No ORM
        # identity or LoginScope is ever rewritten to pretend G+1 is still G.
        expected = {
            "schema_version": 1,
            "kind": "invitation_finalize",
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": str(context.revision_id),
            "config_digest": context.config_digest,
            "identity_id": identity.id,
            "subject_digest": attempt.key.subject_digest,
            "account_id": issuance.account_id,
            "workspace_id": issuance.workspace_id,
            "generation": attempt.expected_generation,
            "fence_epoch": context.fence_epoch,
            "issuance_id": issuance.issuance_id,
            "payload_digest": issuance.payload_digest,
            "lifecycle_id": issuance.lifecycle_id,
            "lifecycle_epoch": issuance.lifecycle_epoch,
            "join_id_at_issue": issuance.join_id_at_issue,
            "observed_join_role": role if issuance.join_id_at_issue is not None else None,
            "operation_id": operation.id,
            "expected_receipt_json": data["expected_receipt_json"],
            "reconcile_until_utc": _deadline(_time(operation.created_at)),
        }
        _require(operation.desired_json == _canonical(expected))
        _require(operation.scope_digest == sha256(operation.desired_json.encode()).hexdigest())
        _require(operation.idempotency_key == operation_idempotency_key(attempt.issuance_id))
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
        _require(operation.kind is CasdoorIntentKind.INVITATION_FINALIZE)
        _require(operation.membership_id is None and operation.ownership_epoch == 0 and operation.attempt_count == 0)
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
        _require(operation.operation_state is CasdoorOperationState.APPLIED)
        _require(operation.termination_state is CasdoorTerminationState.CONFIRMED)
        _require(operation.termination_proof_kind == _PROOF_KIND)
        _require(type(operation.proof_ref) is str and operation.proof_ref.isascii() and len(operation.proof_ref) <= 128)
        match = re.fullmatch(r"v1:([0-9a-f-]{36}):([01]):([1-9][0-9]{0,15}):([0-9a-f]{64})", operation.proof_ref)
        _require(match is not None)
        join_id, created_text, epoch_text, _ = match.groups()
        _uuid(join_id)
        created, epoch = created_text == "1", int(epoch_text)
        _require(epoch <= MAX_LIFECYCLE_EPOCH and epoch == issuance.lifecycle_epoch + int(created))
        _require(created == (issuance.join_id_at_issue is None))
        _require(join.id == join_id and lifecycle.epoch == epoch)
        if created:
            _require(role == issuance.role and role != TenantAccountRole.OWNER)
        else:
            _require(join.id == issuance.join_id_at_issue and role == data["observed_join_role"])
        _require(
            _time(operation.created_at) <= _time(operation.terminated_at) < operation.created_at + _RECONCILE_WINDOW
        )
        _require(operation.terminated_at == _time(operation.updated_at) and operation.terminated_at <= _now())
        _require(operation.created_at <= _time(issuance.consumed_at) <= operation.terminated_at)
        snapshot = InvitationOperationSnapshot(
            UUID(operation.id),
            operation.desired_json,
            operation.scope_digest,
            operation.idempotency_key,
            operation.created_at,
        )
        _require(
            operation.proof_ref
            == _proof(
                snapshot,
                data,
                join_id=join_id,
                join_role=role,
                created=created,
                result_epoch=epoch,
            )
        )
        _require(
            tuple(scope.intents[0])
            == (
                operation.id,
                operation.namespace_id,
                operation.identity_id,
                operation.account_id,
                operation.workspace_id,
                None,
                operation.revision_id,
                operation.kind,
                operation.generation,
                operation.ownership_epoch,
                operation.fence_epoch,
                operation.operation_state,
                operation.termination_state,
            )
        )
        projected_joins = tuple(r for r in scope.joins if r.id == join.id)
        _require(len(projected_joins) == 1)
        projected_join = projected_joins[0]
        _require(all(getattr(join, field) == value for field, value in projected_join._mapping.items()))
        return snapshot

    def _receipt(self, attempt, scope, rows, snapshot):
        lifecycle, issuance, join, operation, audit, histories = rows
        receipt = InvitedLocalWriteReceipt(audit.summary_json)
        value = receipt.values()
        identity, context = scope.identities[0], attempt.context
        _uuid(audit.id)
        _require(operation.terminated_at <= _time(audit.created_at) <= _now())
        _require(audit.actor_account_id is None and audit.result_code == "verified")
        refs = {
            "operation_id": operation.id,
            "issuance_id": issuance.issuance_id,
            "integration_id": str(context.integration_id),
            "namespace_id": str(context.namespace_id),
            "revision_id": str(context.revision_id),
            "identity_id": identity.id,
            "account_id": str(attempt.account_id),
            "workspace_id": str(attempt.workspace_id),
            "invitation_join_id": join.id,
        }
        _require(value["references"] == refs and audit.correlation_id == operation.id and audit.action == RECEIPT_KIND)
        for field in ("namespace_id", "revision_id", "identity_id", "account_id"):
            _require(getattr(audit, field) == refs[field])
        _require(
            (
                value["generation_before"],
                value["generation_after"],
                value["fence_epoch"],
                value["scope_digest"],
                value["completion_proof_ref"],
                value["payload_digest"],
            )
            == (
                attempt.expected_generation,
                identity.sync_generation,
                context.fence_epoch,
                snapshot.scope_digest,
                operation.proof_ref,
                issuance.payload_digest,
            )
        )
        actual_digest = postwrite_sha256(
            operation.id,
            identity=_primitive(identity, IDENTITY_FIELDS),
            joins=tuple(_primitive(row, JOIN_FIELDS) for row in scope.joins),
            memberships=tuple(_primitive(row, MEMBERSHIP_FIELDS) for row in scope.histories),
        )
        _require(value["postwrite_sha256"] == actual_digest)
        self._results(scope, rows, value)
        return receipt

    def _results(self, scope, rows, value):
        """Validate checkable stored outcomes; never recreate historical roles."""
        _lifecycle, _issuance, _join, _operation, audit, histories = rows
        refs, generation = value["references"], value["generation_after"]
        projected = {row.id: row for row in scope.histories}
        _require(set(projected) == {row.id for row in histories})
        details = {row.id: row for row in histories}
        for row in histories:
            _require(all(getattr(row, field) == item for field, item in projected[row.id]._mapping.items()))
            _require(_time(row.created_at) <= _time(row.updated_at) <= _now())
        joins = {row.id: row for row in scope.joins}
        configured = {str(scope.configuration.default_workspace_id)} | {
            str(mapping.workspace_id) for mapping in scope.configuration.workspace_mappings
        }
        results = value["results"]
        _require(str(scope.configuration.default_workspace_id) in {r["workspace_id"] for r in results})
        used = set()
        for item in results:
            _require(item["workspace_id"] in configured)
            join = joins.get(item["join_id"])
            _require(join is not None and join.tenant_id == item["workspace_id"] and join.role == item["current_role"])
            if item["membership_created"]:
                history = details.get(item["membership_id"])
                _require(history is not None and history.id not in used)
                used.add(history.id)
                _require(item["ownership_decision"] == "new_join_required")
                _require(item["outcome"] == "applied" and not item["role_changed"] and item["metadata_changed"])
                _require(
                    (
                        history.namespace_id,
                        history.identity_id,
                        history.account_id,
                        history.workspace_id,
                        history.join_id,
                        history.revision_id,
                        history.desired_generation,
                        history.ownership,
                        history.ownership_epoch,
                        history.finalization,
                        history.tombstone,
                    )
                    == (
                        refs["namespace_id"],
                        refs["identity_id"],
                        refs["account_id"],
                        item["workspace_id"],
                        join.id,
                        refs["revision_id"],
                        generation,
                        CasdoorMembershipOwnership.MANAGED,
                        0,
                        CasdoorFinalizationState.PENDING,
                        False,
                    )
                )
                _require(join.invited_by is None)
                observation = MembershipObservation(
                    UUID(join.tenant_id),
                    UUID(join.account_id),
                    UUID(join.id),
                    join.role,
                    MembershipBackend.LOCAL,
                )
                baseline = role_baseline_json(observation)
                _require(history.baseline_json == baseline and history.last_applied_roles_json == baseline)
                _require(history.last_applied_fingerprint == roles_fingerprint(observation))
                if history.source is CasdoorMembershipSource.FALLBACK:
                    _require(join.tenant_id == str(scope.configuration.default_workspace_id) and join.role == "normal")
                    reason = CasdoorDecisionReason.DEFAULT_NORMAL_FALLBACK
                else:
                    _require(history.source is CasdoorMembershipSource.MAPPING)
                    mapping = next(
                        (m for m in scope.configuration.workspace_mappings if str(m.workspace_id) == join.tenant_id),
                        None,
                    )
                    _require(
                        mapping is not None
                        and join.role in ("admin", "editor", "normal")
                        and getattr(mapping, join.role.value) is not None
                    )
                    reason = CasdoorDecisionReason.ROLE_MAPPING
                desired = {
                    "schema_version": 1,
                    "backend": "local",
                    "target_role": join.role.value,
                    "builtin_id": join.role.value,
                    "role_ids": [join.role.value],
                    "reason": reason.value,
                    "fence_epoch": value["fence_epoch"],
                }
                _require(history.desired_roles_json == _canonical(desired))
            else:
                _require(item["membership_id"] is None and item["outcome"] == "preserved")
                _require(not item["role_changed"] and not item["metadata_changed"])
                _require(
                    item["ownership_decision"]
                    == ("owner_protected" if join.role is TenantAccountRole.OWNER else "preserve_unmanaged")
                )
                _require(not any(row.workspace_id == join.tenant_id for row in histories))
        _require(used == set(details))

    def observe(self, attempt):
        root = self._root(first=True)
        try:
            _attempt(attempt)
            local_policy()
            with self.session.no_autoflush:
                projection = CasdoorLoginScopeRepository(self.session, self.configuration_factory)
                candidate = self._scope(projection, attempt)
                projection._lock_invited_integration(attempt.context)
                _require(self._scope(projection, attempt) == candidate)
                projection._lock_invited_candidate_parents(attempt.context, candidate)
                _require(self._scope(projection, attempt) == candidate)
                rows = self._rows(attempt, lock=True)
                snapshot = self._invitation(attempt, candidate, rows)
                receipt = self._receipt(attempt, candidate, rows, snapshot)
                final = self._scope(projection, attempt)
                _require(final == candidate)
                reread = self._rows(attempt, lock=False)
                _require(reread == rows)
                final_snapshot = self._invitation(attempt, final, reread)
                _require(
                    final_snapshot == snapshot and self._receipt(attempt, final, reread, final_snapshot) == receipt
                )
                self._root(expected=root)
                return InvitedWriteReceiptObservation(
                    receipt,
                    snapshot.operation_id,
                    rows[3].proof_ref,
                    final.identities[0].sync_generation,
                )
        except (
            SQLAlchemyError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            RecursionError,
            StopIteration,
        ):
            raise CasdoorLoginScopeConflict() from None
