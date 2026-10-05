"""Private exact invited LOCAL finalization producer in one fresh SQL root.

The E reader establishes D authority here before any writes. Only this producer's
actual CAS rows may be back-projected for its own final verification. This does
not relax E, complete the invitation barrier, grant admission, or enable F1 retry.
SQLite exercises sequential checks; production contention/phantoms remain open.
"""

from dataclasses import replace
from enum import StrEnum
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID
from weakref import WeakKeyDictionary

import sqlalchemy as sa
from core.casdoor.invited_finalization_receipt import (
    FULL_HISTORY_FIELDS,
    MODE,
    RECEIPT_KIND,
    InvitedLocalFinalizationReceipt,
    canonical,
    finalized_rows_sha256,
    mapping_plan_sha256,
)
from core.casdoor.invited_write_receipt import (
    IDENTITY_FIELDS,
    JOIN_FIELDS,
    MEMBERSHIP_FIELDS,
    postwrite_sha256,
)
from core.casdoor.leases import CasdoorLeases
from core.casdoor.mapping import MappingIdentityContext, resolve_workspace_plan
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorFinalizationState
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from services.casdoor_login_account_service_extend import _check_deadline

from repositories.casdoor_audit_repository_extend import CasdoorAuditRepository
from repositories.casdoor_invitation_finalization_repository_extend import local_policy
from repositories.casdoor_invited_write_receipt_repository_extend import (
    CasdoorInvitedWriteReceiptRepository,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
    CasdoorLoginScopeRepository,
)

# Identity-bound live capability state. Copied owners and copied receipt bytes
# have no entry. Weak keys also avoid retaining a caller-abandoned producer.
_APPEND_PERMITS = WeakKeyDictionary()


def _require(condition):
    if not condition:
        raise CasdoorLoginScopeConflict()


def _primitive(row, fields):
    return {k: getattr(row, k).value if isinstance(getattr(row, k), StrEnum) else getattr(row, k) for k in fields}


def _project_pending(row, ids):
    if row.id not in ids:
        return row
    value = dict(row._mapping, finalization=CasdoorFinalizationState.PENDING)
    return SimpleNamespace(**value, _mapping=value)


def _authorize_finalization_append(owner, session, receipt):
    """Only the exact live producer with verified CAS readback can append once."""
    _require(type(owner) is CasdoorInvitedLocalFinalizationRepository)
    _require(owner.session is session and owner._phase == "append" and owner._receipt is receipt)
    permit = _APPEND_PERMITS.get(owner)
    _require(permit is not None and permit[0] is session and permit[1] is owner._root and permit[2] is receipt)
    owner._reader._root(expected=owner._root)
    del _APPEND_PERMITS[owner]
    owner._phase = "append_started"


class CasdoorInvitedLocalFinalizationRepository:
    def __init__(self, session, configuration_factory):
        self.session = session
        self._reader = CasdoorInvitedWriteReceiptRepository(session, configuration_factory)
        self._projection = CasdoorLoginScopeRepository(session, configuration_factory)
        self._phase = "new"
        self._receipt = None
        self._root = None

    def revoke(self):
        _APPEND_PERMITS.pop(self, None)
        self._phase = "revoked"
        self._receipt = None

    def _leases(self):
        local_policy()
        _require(type(self._leases_value) is CasdoorLeases)
        _require(self._leases_value._deadline == self._deadline)
        _require(self._leases_value.canonical_keys == self._before_scope.lease_scope.canonical_keys)
        _check_deadline(self._deadline)
        self._leases_value.ensure_owned()
        _check_deadline(self._deadline)

    def _parents(self, scope):
        e, a, c = self._reader, self._attempt, self._attempt.context
        return {
            "integration": e._one(Integration, Integration.id == str(c.integration_id)),
            "revision": e._one(Revision, Revision.id == str(c.revision_id)),
            "namespaces": e._bounded_rows(Namespace, Namespace.integration_id == str(c.integration_id), cap=2000),
            "account": e._one(Account, Account.id == str(a.account_id)),
            "identities": e._bounded_rows(Identity, Identity.account_id == str(a.account_id), cap=2048),
            "workspaces": e._bounded_rows(Tenant, Tenant.id.in_([r.id for r in scope.workspaces]), cap=2048),
            "joins": e._bounded_rows(Join, Join.account_id == str(a.account_id), cap=2048),
        }

    def _plan(self, scope):
        a, c = self._attempt, self._attempt.context
        context = MappingIdentityContext(
            c.integration_id,
            c.revision_id,
            c.namespace_id,
            UUID(scope.identities[0].id),
            a.account_id,
            c.config_digest,
            c.issuer,
            c.organization,
            c.application,
            c.client_id,
            c.subject,
        )
        plan = resolve_workspace_plan(
            configuration=scope.configuration, snapshot=self._roles, context=context, availability=scope.availability()
        )
        values = tuple(
            {
                "workspace_id": str(t.workspace_id),
                "target_role": t.target_role,
                "builtin_id": t.builtin_id,
                "reason": t.reason.value,
            }
            for t in plan.targets
        )
        _require([v["workspace_id"] for v in values] == [r["workspace_id"] for r in self._d["results"]])
        return values

    def _targets(self, scope, rows, plan):
        """E proved current PENDING/history/ownership; bind it to current roles."""
        histories = {r.id: r for r in rows[5]}
        targets = []
        for result, target in zip(self._d["results"], plan, strict=True):
            if result["membership_created"] or result["membership_regranted"]:
                history = histories.get(result["membership_id"])
                _require(history is not None and target["target_role"] == result["current_role"])
                desired = canonical(
                    {
                        "schema_version": 1,
                        "backend": "local",
                        "target_role": target["target_role"],
                        "builtin_id": target["builtin_id"],
                        "role_ids": [target["target_role"]],
                        "reason": target["reason"],
                        "fence_epoch": self._d["fence_epoch"],
                    }
                )
                _require(history.desired_roles_json == desired)
                targets.append(history)
        for item in self._d.get("withdrawals", ()):
            history = histories.get(item["membership_id"])
            _require(history is not None and item["workspace_id"] not in {t["workspace_id"] for t in plan})
            _require(history.finalization is CasdoorFinalizationState.PENDING)
            targets.append(history)
        created_ids = {r.id for r in targets}
        _require(
            all(
                row.finalization is CasdoorFinalizationState.FINALIZED
                for row in histories.values()
                if row.id not in created_ids
            )
        )
        for result, target in zip(self._d["results"], plan, strict=True):
            if result["ownership_decision"] == "managed_current":
                _require(result["current_role"] == target["target_role"])
                history = histories[result["membership_id"]]
                _require(
                    history.desired_roles_json
                    == canonical(
                        {
                            "schema_version": 1,
                            "backend": "local",
                            "target_role": target["target_role"],
                            "builtin_id": target["builtin_id"],
                            "role_ids": [target["target_role"]],
                            "reason": target["reason"],
                            "fence_epoch": self._d["fence_epoch"],
                        }
                    )
                )
        return tuple(sorted(targets, key=lambda r: r.id))

    def _cas(self, row):
        """Exact all-column predicate; only finalization plus model timestamp effect."""
        _require(set(row._mapping) == set(FULL_HISTORY_FIELDS) == set(History.__table__.columns.keys()))
        _require(row.finalization is CasdoorFinalizationState.PENDING)
        conditions = [
            getattr(History, k).is_(None) if v is None else getattr(History, k) == v for k, v in row._mapping.items()
        ]
        result = self.session.execute(
            sa.update(History)
            .where(*conditions)
            .values(finalization=CasdoorFinalizationState.FINALIZED)
            .execution_options(synchronize_session=False)
        )
        _require(result.rowcount == 1)
        after = self._reader._one(History, History.id == row.id)
        expected = dict(row._mapping, finalization=CasdoorFinalizationState.FINALIZED, updated_at=after.updated_at)
        _require(dict(after._mapping) == expected and row.updated_at <= after.updated_at)
        return after

    def _digest(self, scope):
        return postwrite_sha256(
            self._d["references"]["operation_id"],
            identity=_primitive(scope.identities[0], IDENTITY_FIELDS),
            joins=tuple(_primitive(r, JOIN_FIELDS) for r in scope.joins),
            memberships=tuple(_primitive(r, MEMBERSHIP_FIELDS) for r in scope.histories),
        )

    def _reread(self):
        """SQL only: entire current scope, full rows, original D and exact F audit."""
        e, a = self._reader, self._attempt
        local_policy()
        e._root(expected=self._root)
        current = e._scope(self._projection, a)
        rows = e._rows(a, lock=False)
        _require(rows[:5] == self._before_rows[:5] and self._parents(current) == self._before_parents)
        _require(tuple(rows[5]) == self._after_histories)
        pending = replace(current, histories=tuple(_project_pending(r, self._ids) for r in current.histories))
        _require(replace(current, histories=self._before_scope.histories) == self._before_scope)
        _require(
            tuple(dict(r._mapping) for r in pending.histories)
            == tuple(dict(r._mapping) for r in self._before_scope.histories)
        )
        pending_rows = (*rows[:5], tuple(_project_pending(r, self._ids) for r in rows[5]))
        snapshot = e._invitation(a, pending, pending_rows)
        _require(e._receipt(a, pending, pending_rows, snapshot) == self._observation.receipt)
        _require(self._plan(current) == self._plan_values)
        _require(self._digest(current) == self._receipt.values()["postwrite_sha256"])
        targets = tuple(_primitive(r, FULL_HISTORY_FIELDS) for r in rows[5] if r.id in self._ids)
        _require(
            finalized_rows_sha256(self._d["references"]["operation_id"], targets)
            == self._receipt.values()["finalized_rows_sha256"]
        )
        CasdoorAuditRepository(self.session)._read_invited_finalization_receipt(self._receipt, self._audit_expected)
        e._root(expected=self._root)

    def produce(self, attempt, *, roles, leases, deadline):
        """No commit here; caller commits immediately after this final SQL barrier."""
        _require(self._phase == "new" and type(roles) is EffectiveRoleSnapshot and type(leases) is CasdoorLeases)
        _check_deadline(deadline)
        self._phase = "observing"
        self._attempt, self._roles, self._leases_value, self._deadline = attempt, roles, leases, deadline
        # observe() must remain this root's first SQL owner. Never use E service.
        self._observation = self._reader.observe(attempt)
        self._root = self.session.get_transaction()
        self._d = self._observation.receipt.values()
        self._before_scope = self._reader._scope(self._projection, attempt)
        self._before_rows = self._reader._rows(attempt, lock=False)
        self._before_parents = self._parents(self._before_scope)
        _require(
            self._reader._receipt(
                attempt,
                self._before_scope,
                self._before_rows,
                self._reader._invitation(attempt, self._before_scope, self._before_rows),
            )
            == self._observation.receipt
        )
        self._plan_values = self._plan(self._before_scope)
        targets = self._targets(self._before_scope, self._before_rows, self._plan_values)
        operation_id = self._d["references"]["operation_id"]
        _require(
            not self._reader._bounded_rows(
                Audit, Audit.action == RECEIPT_KIND, Audit.correlation_id == operation_id, cap=0
            )
        )
        self._leases()
        finalized = tuple(self._cas(row) for row in targets)
        self.session.flush()
        self._ids = frozenset(r.id for r in finalized)
        by_id = {row.id: row for row in finalized}
        self._after_histories = tuple(by_id.get(row.id, row) for row in self._before_rows[5])
        current = self._reader._scope(self._projection, attempt)
        self._receipt = InvitedLocalFinalizationReceipt.from_values(
            {
                "schema_version": 1,
                "receipt_kind": RECEIPT_KIND,
                "mode": MODE,
                "references": self._d["references"],
                "generation": self._d["generation_after"],
                "fence_epoch": self._d["fence_epoch"],
                "write_receipt_sha256": sha256(self._observation.receipt.canonical_json.encode("utf-8")).hexdigest(),
                "before_postwrite_sha256": self._d["postwrite_sha256"],
                "plan_sha256": mapping_plan_sha256(operation_id, self._plan_values),
                "finalized_ids": sorted(self._ids),
                "finalized_rows_sha256": finalized_rows_sha256(
                    operation_id, tuple(_primitive(r, FULL_HISTORY_FIELDS) for r in finalized)
                ),
                "postwrite_sha256": self._digest(current),
            }
        )
        self._phase = "append"
        _APPEND_PERMITS[self] = (self.session, self._root, self._receipt)
        audit = CasdoorAuditRepository(self.session)._append_invited_finalization_receipt(
            self._receipt, finalization_owner=self
        )
        self._audit_expected = dict(audit._mapping)
        self._phase = "appended"
        self._reread()
        # LAST external I/O. Full SQL revalidation follows; there is no later lease call.
        self._leases()
        self._reread()
        _check_deadline(deadline)
        return self._receipt
