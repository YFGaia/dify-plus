"""Fresh SQL-only recovery of an already committed invited LOCAL finalization.

No producer, capability, lease or retry is involved. D's whitelist and F's full
history digest bind only their documented columns. Other full rows are checked
for current first/last consistency, not equality to the historical commit. This
assumes trusted SQL persistence; SQLite tests prove sequential behavior only.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from uuid import UUID

from core.casdoor.claims import StructuredUserRef
from core.casdoor.invited_finalization_receipt import (
    FULL_HISTORY_FIELDS,
    MAX_RECEIPT_BYTES,
    RECEIPT_KIND,
    InvitedLocalFinalizationReceipt,
    canonical,
    finalized_rows_sha256,
    mapping_plan_sha256,
)
from core.casdoor.invited_write_receipt import IDENTITY_FIELDS, JOIN_FIELDS, MEMBERSHIP_FIELDS, postwrite_sha256
from core.casdoor.mapping import MappingIdentityContext, _text, _valid_ref, resolve_workspace_plan
from core.casdoor.role_graph import MAX_NODES, EffectiveRoleSnapshot
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from sqlalchemy.exc import SQLAlchemyError

from repositories.casdoor_invitation_finalization_repository_extend import local_policy
from repositories.casdoor_invitation_operation_repository_extend import _attempt, _now, _uuid
from repositories.casdoor_invited_write_receipt_repository_extend import CasdoorInvitedWriteReceiptRepository, _time
from repositories.casdoor_login_scope_repository_extend import (
    MAX_SCOPE_ROWS,
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)


@dataclass(frozen=True, slots=True, repr=False)
class InvitedFinalizationObservation:
    """Current consistency evidence, stale after rollback; grants no authority."""

    operation_id: UUID
    current_generation: int
    write_summary_sha256: str
    finalization_summary_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class _PendingProjection:
    """Immutable scalar copy; never an ORM entity or a historical timestamp edit."""

    _mapping: MappingProxyType

    def __getattr__(self, name):
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(name) from None


def _require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


def _inputs(attempt, roles):
    """Exact private DTOs and bounded role primitives before any SQL read."""
    _attempt(attempt)
    _require(type(roles) is EffectiveRoleSnapshot)
    _require(
        type(roles.subject) is str
        and roles.subject == attempt.context.subject
        and type(roles.user_ref) is StructuredUserRef
        and roles.user_ref.owner == attempt.context.organization
        and _text(roles.user_ref.name, 255)
        and type(roles.effective_roles) is tuple
        and len(roles.effective_roles) <= MAX_NODES
        and all(_valid_ref(ref, attempt.context.organization) for ref in roles.effective_roles)
        and len(frozenset(roles.effective_roles)) == len(roles.effective_roles)
    )


def _primitive(row, fields):
    return {k: getattr(row, k).value if isinstance(getattr(row, k), StrEnum) else getattr(row, k) for k in fields}


def _pending(row, ids):
    if row.id not in ids:
        return row
    _require(row.finalization is CasdoorFinalizationState.FINALIZED)
    return _PendingProjection(MappingProxyType(dict(row._mapping, finalization=CasdoorFinalizationState.PENDING)))


class CasdoorInvitedFinalizationReceiptRepository:
    def __init__(self, session, configuration_factory):
        self.session = session
        self._reader = CasdoorInvitedWriteReceiptRepository(session, configuration_factory)
        self._projection = CasdoorLoginScopeRepository(session, configuration_factory)

    def _parents(self, attempt, scope):
        e, c = self._reader, attempt.context
        return {
            "integration": e._one(Integration, Integration.id == str(c.integration_id)),
            "revision": e._one(Revision, Revision.id == str(c.revision_id)),
            "namespaces": e._bounded_rows(Namespace, Namespace.integration_id == str(c.integration_id), cap=2000),
            "account": e._one(Account, Account.id == str(attempt.account_id)),
            "identities": e._bounded_rows(Identity, Identity.account_id == str(attempt.account_id), cap=MAX_SCOPE_ROWS),
            "workspaces": e._bounded_rows(Tenant, Tenant.id.in_([r.id for r in scope.workspaces]), cap=MAX_SCOPE_ROWS),
            "joins": e._bounded_rows(Join, Join.account_id == str(attempt.account_id), cap=MAX_SCOPE_ROWS),
        }

    def _f_row(self, operation_id, *, lock):
        return self._reader._one(
            Audit,
            Audit.action == RECEIPT_KIND,
            Audit.correlation_id == operation_id,
            lock=lock,
            text_limit=MAX_RECEIPT_BYTES,
        )

    def _plan(self, attempt, scope, roles):
        c = attempt.context
        context = MappingIdentityContext(
            c.integration_id,
            c.revision_id,
            c.namespace_id,
            UUID(scope.identities[0].id),
            attempt.account_id,
            c.config_digest,
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.subject,
        )
        plan = resolve_workspace_plan(
            configuration=scope.configuration, snapshot=roles, context=context, availability=scope.availability()
        )
        return tuple(
            {
                "workspace_id": str(t.workspace_id),
                "target_role": t.target_role,
                "builtin_id": t.builtin_id,
                "reason": t.reason.value,
            }
            for t in plan.targets
        )

    def _verify(self, attempt, roles, scope, rows, audit):
        """Verify actual FINALIZED state before a narrow immutable D projection."""
        e = self._reader
        f_receipt = InvitedLocalFinalizationReceipt(audit.summary_json)
        f = f_receipt.values()
        ids = frozenset(f["finalized_ids"])
        _require(ids.issubset({r.id for r in rows[5]}) and {r.id for r in rows[5]} == {r.id for r in scope.histories})
        _require(len(rows[5]) == len(scope.histories))
        _require(all(r.finalization is CasdoorFinalizationState.FINALIZED for r in rows[5]))
        operation_id = rows[3].id
        _require(
            finalized_rows_sha256(
                operation_id, tuple(_primitive(r, FULL_HISTORY_FIELDS) for r in rows[5] if r.id in ids)
            )
            == f["finalized_rows_sha256"]
        )
        pending_scope = replace(scope, histories=tuple(_pending(r, ids) for r in scope.histories))
        pending_rows = (*rows[:5], tuple(_pending(r, ids) for r in rows[5]))
        snapshot = e._invitation(attempt, pending_scope, pending_rows)
        d_receipt = e._receipt(attempt, pending_scope, pending_rows, snapshot)
        d = d_receipt.values()
        from core.casdoor.invited_controlled_write_receipt import controlled_finalization_ids

        created_ids = controlled_finalization_ids(d)
        _require(f["finalized_ids"] == created_ids and len(created_ids) == len(ids))
        _require(f["references"] == d["references"])
        _require((f["generation"], f["fence_epoch"]) == (d["generation_after"], d["fence_epoch"]))
        _require(f["write_receipt_sha256"] == sha256(d_receipt.canonical_json.encode("utf-8")).hexdigest())
        _require(f["before_postwrite_sha256"] == d["postwrite_sha256"])
        _uuid(audit.id)
        _require(_time(rows[4].created_at) <= _time(audit.created_at) <= _now())
        _require(audit.actor_account_id is None and audit.result_code == "verified")
        _require(audit.action == RECEIPT_KIND and audit.correlation_id == operation_id)
        _require(
            all(
                getattr(audit, field) == d["references"][field]
                for field in ("namespace_id", "revision_id", "identity_id", "account_id")
            )
        )
        digest = postwrite_sha256(
            operation_id,
            identity=_primitive(scope.identities[0], IDENTITY_FIELDS),
            joins=tuple(_primitive(r, JOIN_FIELDS) for r in scope.joins),
            memberships=tuple(_primitive(r, MEMBERSHIP_FIELDS) for r in scope.histories),
        )
        _require(digest == f["postwrite_sha256"])
        plan = self._plan(attempt, scope, roles)
        _require([t["workspace_id"] for t in plan] == [r["workspace_id"] for r in d["results"]])
        _require(mapping_plan_sha256(operation_id, plan) == f["plan_sha256"])
        histories = {r.id: r for r in rows[5]}
        for result, target in zip(d["results"], plan, strict=True):
            if (
                result["membership_created"]
                or result["membership_regranted"]
                or result["ownership_decision"] == "managed_current"
            ):
                _require(result["current_role"] == target["target_role"])
                _require(
                    histories[result["membership_id"]].desired_roles_json
                    == canonical(
                        {
                            "schema_version": 1,
                            "backend": "local",
                            "target_role": target["target_role"],
                            "builtin_id": target["builtin_id"],
                            "role_ids": [target["target_role"]],
                            "reason": target["reason"],
                            "fence_epoch": d["fence_epoch"],
                        }
                    )
                )
        _require(
            all(item["workspace_id"] not in {t["workspace_id"] for t in plan} for item in d.get("withdrawals", ()))
        )
        return InvitedFinalizationObservation(
            UUID(operation_id),
            scope.identities[0].sync_generation,
            f["write_receipt_sha256"],
            sha256(f_receipt.canonical_json.encode("utf-8")).hexdigest(),
        )

    def observe(self, attempt, *, roles):
        try:
            _inputs(attempt, roles)
            local_policy()
            e, p = self._reader, self._projection
            root = e._root(first=True)
            with self.session.no_autoflush:
                candidate = e._scope(p, attempt)
                p._lock_invited_integration(attempt.context)
                _require(e._scope(p, attempt) == candidate)
                p._lock_invited_candidate_parents(attempt.context, candidate)
                _require(e._scope(p, attempt) == candidate)
                rows = e._rows(attempt, lock=True)
                audit = self._f_row(rows[3].id, lock=True)
                parents = self._parents(attempt, candidate)
                observation = self._verify(attempt, roles, candidate, rows, audit)
                local_policy()
                current = e._scope(p, attempt)
                reread = e._rows(attempt, lock=False)
                final_audit = self._f_row(rows[3].id, lock=False)
                _require(current == candidate and reread == rows and final_audit == audit)
                _require(self._parents(attempt, current) == parents)
                _require(self._verify(attempt, roles, current, reread, final_audit) == observation)
                e._root(expected=root)
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
