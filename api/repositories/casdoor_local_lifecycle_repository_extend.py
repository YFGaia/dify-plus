"""LOCAL/no-intent maintenance under caller leases and ordered parent locks.

Review snapshots and release receipts are reconstructed from actual SQL owners.
No public confirmation, role match, queue state or terminal intent is authority.
Provider work belongs to the service and must finish before this short root UoW.
"""

import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from dataclasses import dataclass, field as dataclass_field
from uuid import UUID, uuid4

import sqlalchemy as sa
from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    parse_role_baseline_json,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole, TenantStatus
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend as Validation,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)

MAX_ROWS = 100
MAX_TEXT = 65535
RELEASE_ACTION = "local_membership_release_v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail():
    raise AuthTransactionError("local_lifecycle_pending")


@dataclass(frozen=True, repr=False)
class LocalLifecycleScope:
    integration_id: UUID
    namespace_id: UUID
    identity_id: UUID
    account_id: UUID
    workspace_id: UUID
    subject: str
    etag: int
    fence_epoch: int
    identity_generation: int
    active_revision_id: UUID | None
    join_id: UUID
    current_role: TenantAccountRole
    histories: tuple[dict, ...]
    _archived_facts: tuple[str, ...] = dataclass_field(default=(), repr=False)
    _archived_subjects: tuple[tuple[str, str], ...] = dataclass_field(default=(), repr=False)
    _historical_workspace_ids: tuple[str, ...] = dataclass_field(default=(), repr=False)

    @property
    def fingerprint(self):
        return digest(
            {
                "integration": str(self.integration_id),
                "namespace": str(self.namespace_id),
                "identity": str(self.identity_id),
                "account": str(self.account_id),
                "workspace": str(self.workspace_id),
                "subject_sha256": hashlib.sha256(self.subject.encode()).hexdigest(),
                "etag": self.etag,
                "fence": self.fence_epoch,
                "generation": self.identity_generation,
                "active_revision": str(self.active_revision_id) if self.active_revision_id else None,
                "join": str(self.join_id),
                "role": self.current_role.value,
                "histories": self.histories,
                "archived_facts": self._archived_facts,
                "archived_subjects": self._archived_subjects,
                "historical_workspaces": self._historical_workspace_ids,
            }
        )


class CasdoorLocalLifecycleRepository:
    def __init__(self, session):
        self.session = session
        self._preparations = {}

    def candidates(self, *, after_identity_id=None, after_workspace_id=None, limit=20):
        """Bounded manager-only navigation, never lifecycle permission/proof."""
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or (after_identity_id is None) != (after_workspace_id is None)
        ):
            _fail()
        query = sa.select(
            Identity.id.label("identity_id"),
            Tenant.id.label("workspace_id"),
            Account.id.label("account_id"),
            sa.case((sa.func.length(Account.name) <= 255, Account.name), else_=None).label("account_name"),
            sa.case((sa.func.length(Tenant.name) <= 255, Tenant.name), else_=None).label("workspace_name"),
            TenantAccountJoin.role,
        )
        query = query.select_from(Integration).join(Namespace, Namespace.integration_id == Integration.id)
        query = query.join(Identity, Identity.namespace_id == Namespace.id).join(
            Account, Account.id == Identity.account_id
        )
        query = query.join(TenantAccountJoin, TenantAccountJoin.account_id == Account.id).join(
            Tenant, Tenant.id == TenantAccountJoin.tenant_id
        )
        query = query.where(
            Integration.slot == 1, Account.status == AccountStatus.ACTIVE, Tenant.status == TenantStatus.NORMAL
        )
        if after_identity_id is not None:
            query = query.where(
                sa.or_(
                    Identity.id > str(after_identity_id),
                    sa.and_(Identity.id == str(after_identity_id), Tenant.id > str(after_workspace_id)),
                )
            )
        with self.session.no_autoflush:
            rows = self._rows(query.order_by(Identity.id, Tenant.id).limit(limit + 1), lock=False)
        selected = rows[:limit]
        return {
            "items": [
                {
                    "identity_id": UUID(row["identity_id"]),
                    "workspace_id": UUID(row["workspace_id"]),
                    "account_id": UUID(row["account_id"]),
                    "account_name": row["account_name"] or row["account_id"],
                    "workspace_name": row["workspace_name"] or row["workspace_id"],
                    "current_role": row["role"].value,
                }
                for row in selected
            ],
            "has_more": len(rows) > limit,
            "next_identity_id": UUID(selected[-1]["identity_id"]) if len(rows) > limit else None,
            "next_workspace_id": UUID(selected[-1]["workspace_id"]) if len(rows) > limit else None,
        }

    def _rows(self, statement, *, lock):
        rows = tuple(self.session.execute(statement.with_for_update() if lock else statement).mappings())
        if len(rows) > MAX_ROWS:
            _fail()
        return rows

    def _history(self, account_id, *, lock):
        predicate = History.account_id == str(account_id)
        lengths = self._rows(
            sa.select(
                History.id,
                *(
                    sa.func.length(sa.cast(getattr(History, key), sa.LargeBinary)).label(key)
                    for key in ("baseline_json", "last_applied_roles_json", "desired_roles_json")
                ),
            )
            .where(predicate)
            .order_by(History.id)
            .limit(MAX_ROWS + 1),
            lock=lock,
        )
        if any(
            any(
                row[key] is None or row[key] > MAX_TEXT
                for key in ("baseline_json", "last_applied_roles_json", "desired_roles_json")
            )
            for row in lengths
        ):
            _fail()
        fields = (
            "id",
            "namespace_id",
            "identity_id",
            "account_id",
            "workspace_id",
            "join_id",
            "ownership",
            "ownership_epoch",
            "source",
            "desired_generation",
            "revision_id",
            "last_applied_roles_json",
            "last_applied_fingerprint",
            "desired_roles_json",
            "baseline_json",
            "finalization",
            "tombstone",
        )
        texts = {"baseline_json", "last_applied_roles_json", "desired_roles_json"}
        columns = [
            sa.case(
                (sa.func.length(sa.cast(getattr(History, key), sa.LargeBinary)) <= MAX_TEXT, getattr(History, key)),
                else_=None,
            ).label(key)
            if key in texts
            else getattr(History, key)
            for key in fields
        ]
        rows = self._rows(sa.select(*columns).where(predicate).order_by(History.id).limit(MAX_ROWS + 1), lock=lock)
        result = []
        for item in rows:
            row = item
            try:
                baseline = parse_role_baseline_json(row["baseline_json"])
                applied = parse_role_baseline_json(row["last_applied_roles_json"])
                if baseline.backend is not MembershipBackend.LOCAL or applied.backend is not MembershipBackend.LOCAL:
                    _fail()
                if (
                    row["tombstone"]
                    or row["finalization"] is not CasdoorFinalizationState.FINALIZED
                    or type(row["ownership_epoch"]) is not int
                    or not 0 <= row["ownership_epoch"] < 2**63 - 1
                ):
                    _fail()
                result.append({key: row[key] for key in fields})
            except (ValueError, TypeError, AttributeError):
                _fail()
        return tuple(result)

    def inspect(self, identity_id, workspace_id, *, source_account_id, lock=False):
        """Discover first, then re-read with integration→namespace→account locks.

        The service compares this exact fingerprint after acquiring the complete
        subject/account/member leases. Any changed scope requires a fresh review.
        """
        with self.session.no_autoflush:
            integrations = self._rows(
                sa.select(Integration.id, Integration.etag, Integration.active_revision_id).where(Integration.slot == 1),
                lock=lock,
            )
            if len(integrations) != 1:
                _fail()
            integration = integrations[0]
            namespaces = self._rows(
                sa.select(
                    Namespace.id,
                    Namespace.integration_id,
                    Namespace.fence_epoch,
                    Namespace.expected_issuer,
                    Namespace.organization,
                )
                .where(Namespace.integration_id == integration["id"])
                .order_by(Namespace.id)
                .limit(MAX_ROWS + 1),
                lock=lock,
            )
            discovery = self.session.execute(
                sa.select(Identity.account_id).where(Identity.id == str(identity_id))
            ).scalar_one_or_none()
            if discovery is None:
                _fail()
            accounts = self._rows(
                sa.select(Account.id, Account.status)
                .where(Account.id.in_(sorted({discovery, str(source_account_id)})))
                .order_by(Account.id),
                lock=lock,
            )
            if len(accounts) != len({discovery, str(source_account_id)}) or any(
                row["status"] is not AccountStatus.ACTIVE for row in accounts
            ):
                _fail()
            identities = self._rows(
                sa.select(
                    Identity.id,
                    Identity.account_id,
                    Identity.namespace_id,
                    Identity.subject,
                    Identity.issuer,
                    Identity.organization,
                    Identity.sync_generation,
                )
                .where(Identity.account_id == discovery)
                .order_by(Identity.id)
                .limit(MAX_ROWS + 1),
                lock=lock,
            )
            # Initial maintenance domain has one exact binding, no old namespace ambiguity.
            selected_identity = [row for row in identities if row["id"] == str(identity_id)]
            if len(selected_identity) != 1:
                _fail()
            identity = selected_identity[0]
            ns = next((row for row in namespaces if row["id"] == identity["namespace_id"]), None)
            if ns is None or (identity["issuer"], identity["organization"]) != (
                ns["expected_issuer"],
                ns["organization"],
            ):
                _fail()
            history_scopes = self._rows(
                sa.select(History.workspace_id)
                .where(History.account_id == discovery)
                .order_by(History.workspace_id)
                .limit(MAX_ROWS + 1),
                lock=False,
            )
            workspace_ids = sorted({str(workspace_id)} | {row["workspace_id"] for row in history_scopes})
            workspaces = self._rows(
                sa.select(Tenant.id, Tenant.status).where(Tenant.id.in_(workspace_ids)).order_by(Tenant.id), lock=lock
            )
            if {row["id"] for row in workspaces} != set(workspace_ids) or any(
                row["status"] is not TenantStatus.NORMAL for row in workspaces
            ):
                _fail()
            archive = self._archived_release_facts(
                account_id=discovery, namespace_id=identity["namespace_id"], identity_id=identity["id"], lock=False
            )
            all_joins = self._rows(
                sa.select(TenantAccountJoin.id, TenantAccountJoin.tenant_id, TenantAccountJoin.role)
                .where(TenantAccountJoin.account_id == discovery, TenantAccountJoin.tenant_id.in_(workspace_ids))
                .order_by(TenantAccountJoin.tenant_id),
                lock=lock,
            )
            joins = tuple(row for row in all_joins if row["tenant_id"] == str(workspace_id))
            if len(joins) != 1 or joins[0]["role"] not in (
                TenantAccountRole.ADMIN,
                TenantAccountRole.EDITOR,
                TenantAccountRole.NORMAL,
            ):
                _fail()
            histories = self._archived_history(UUID(discovery), lock=lock)
            if {row["workspace_id"] for row in histories} != {row["workspace_id"] for row in history_scopes}:
                _fail()
            if (
                lock
                and self._archived_release_facts(
                    account_id=discovery, namespace_id=identity["namespace_id"], identity_id=identity["id"], lock=True
                )
                != archive
            ):
                _fail()
            histories = tuple(row for row in histories if row["id"] not in archive[3])
            retired, _reassignments, _retired_facts = self._retired_responsibility(
                account_id=discovery, namespace_id=identity["namespace_id"], selected_identity=identity, lock=lock,
            )
            if any(row["namespace_id"] != ns["id"] or (
                row["identity_id"] != identity["id"] and row["id"] not in retired
            ) for row in histories):
                _fail()
            revisions = self._rows(
                sa.select(Revision.id, Revision.integration_id, Revision.namespace_id)
                .where(Revision.id.in_([row["revision_id"] for row in histories]))
                .order_by(Revision.id),
                lock=lock,
            )
            if any(
                not any(
                    rev["id"] == row["revision_id"]
                    and rev["integration_id"] == integration["id"]
                    and rev["namespace_id"] == ns["id"]
                    for rev in revisions
                )
                for row in histories
            ):
                _fail()
            self.require_no_intents(UUID(discovery), identity_id, lock=lock)
            scoped = tuple(row for row in histories if row["workspace_id"] == str(workspace_id))
            if len(scoped) > 1 or any(row["join_id"] != joins[0]["id"] for row in scoped):
                _fail()
            archived_facts = archive[0]
            if archived_facts:
                for row in histories:
                    if row["source"] is CasdoorMembershipSource.ADOPT and row["identity_id"] == identity["id"]:
                        _current_id, facts = self._adopted_current_refs(
                            account_id=discovery,
                            namespace_id=identity["namespace_id"],
                            identity_id=identity["id"],
                            workspace_id=row["workspace_id"],
                            lock=lock,
                        )
                        archived_facts += facts[len(archive[0]) :]
            scope = LocalLifecycleScope(
                UUID(integration["id"]),
                UUID(ns["id"]),
                UUID(identity["id"]),
                UUID(discovery),
                workspace_id,
                identity["subject"],
                integration["etag"],
                ns["fence_epoch"],
                identity["sync_generation"],
                UUID(integration["active_revision_id"]) if integration["active_revision_id"] else None,
                UUID(joins[0]["id"]),
                joins[0]["role"],
                histories,
                archived_facts,
                archive[1],
                archive[2],
            )
            if lock:
                self._preparations[id(scope)] = (scope.fingerprint, self.session.get_transaction())
            return scope

    def require_no_intents(self, account_id, identity_id, *, lock):
        rows = self._rows(
            sa.select(Intent.id)
            .where(sa.or_(Intent.account_id == str(account_id), Intent.identity_id == str(identity_id)))
            .order_by(Intent.id)
            .limit(1),
            lock=lock,
        )
        if rows:
            _fail()

    def release(self, scope, *, actor_account_id, correlation_id):
        """Actual CAS producer of retained LOCAL release receipt; no role mutation."""
        issued = self._preparations.pop(id(scope), None)
        if (
            issued is None
            or issued != (scope.fingerprint, self.session.get_transaction())
            or not self.session.in_transaction()
            or self.session.in_nested_transaction()
            or self.session.new
            or self.session.dirty
            or self.session.deleted
        ):
            _fail()
        self.final_join(scope)
        scoped = [row for row in scope.histories if row["workspace_id"] == str(scope.workspace_id)]
        if len(scoped) != 1 or scoped[0]["ownership"] not in (
            CasdoorMembershipOwnership.MANAGED,
            CasdoorMembershipOwnership.LOCAL_OVERRIDE,
        ):
            _fail()
        row = scoped[0]
        result = self.session.execute(
            sa.update(History)
            .where(
                History.id == row["id"],
                History.ownership == row["ownership"],
                History.ownership_epoch == row["ownership_epoch"],
                History.join_id == str(scope.join_id),
                History.desired_generation == row["desired_generation"],
            )
            .values(ownership=CasdoorMembershipOwnership.RELEASED, ownership_epoch=row["ownership_epoch"] + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            _fail()
        released = dict(row, ownership=CasdoorMembershipOwnership.RELEASED, ownership_epoch=row["ownership_epoch"] + 1)
        receipt = {
            "schema_version": 1,
            "membership_id": row["id"],
            "workspace_id": str(scope.workspace_id),
            "join_id": str(scope.join_id),
            "ownership_epoch": released["ownership_epoch"],
            "row_sha256": digest(released),
            "review_sha256": scope.fingerprint,
            "retained_role": scope.current_role.value,
        }
        self.session.add(
            Audit(
                id=str(uuid4()),
                namespace_id=str(scope.namespace_id),
                revision_id=row["revision_id"],
                identity_id=str(scope.identity_id),
                account_id=str(scope.account_id),
                actor_account_id=str(actor_account_id),
                action=RELEASE_ACTION,
                result_code="success",
                correlation_id=str(correlation_id),
                summary_json=canonical(receipt),
            )
        )
        self.session.flush()
        return receipt

    def adopt(self, scope, *, revision_id, target_role, reason, actor_account_id, correlation_id):
        """Server-reviewed actual LOCAL adoption; preserves the original baseline."""
        from repositories.casdoor_local_role_repository_extend import CasdoorLocalRoleRepository
        from repositories.invitation_authority_repository_extend import InvitationAuthorityRepository

        issued = self._preparations.pop(id(scope), None)
        if (
            issued is None
            or issued != (scope.fingerprint, self.session.get_transaction())
            or not self.session.in_transaction()
            or self.session.in_nested_transaction()
            or self.session.new
            or self.session.dirty
            or self.session.deleted
            or scope.active_revision_id != revision_id
        ):
            _fail()
        role = TenantAccountRole(target_role)
        selected = [row for row in scope.histories if row["workspace_id"] == str(scope.workspace_id)]
        if len(selected) > 1 or (selected and selected[0]["ownership"] is CasdoorMembershipOwnership.MANAGED):
            _fail()
        retired, _records, _retired_facts = self._retired_responsibility(
            account_id=scope.account_id, namespace_id=scope.namespace_id,
            selected_identity=next(iter(self._archived_rows(Identity, Identity.id == str(scope.identity_id)))), lock=True,
        )
        reassignment = retired.get(selected[0]["id"]) if selected and selected[0]["identity_id"] != str(scope.identity_id) else None
        if reassignment is not None and (
            selected[0]["namespace_id"] != str(scope.namespace_id) or scope.identity_generation != 0
            or {key: reassignment["before"][key] for key in self._retired_history_fields()} != selected[0]
        ):
            _fail()
        adoption_audit_id = str(uuid4())
        if reassignment is not None:
            # Reject the complete bounded proof before generation, role or History CAS.
            budget = self._reassignment_summary(reassignment, scope=scope, role=role,
                adoption_audit_id=adoption_audit_id, adoption_sha256="0" * 64)
            if len(budget.encode()) > MAX_TEXT:
                _fail()
        generation = scope.identity_generation
        if scope._archived_facts and (not selected or reassignment is not None) and generation == 0:
            # The exact issued scope and arguments belong to the original D09
            # source/review/directory/preview stack. A plan's type grants nothing.
            fresh = self._archived_release_facts(
                account_id=scope.account_id, namespace_id=scope.namespace_id, identity_id=scope.identity_id
            )
            if fresh[0] != scope._archived_facts:
                _fail()
            from core.casdoor.mapping import MappingIdentityContext, DesiredWorkspacePlan, DesiredWorkspaceTarget
            from core.casdoor.errors import CasdoorDecisionReason
            from repositories.casdoor_generation_repository_extend import CasdoorGenerationRepository

            revision = self.session.execute(
                sa.select(
                    Revision.integration_id,
                    Revision.namespace_id,
                    Revision.config_digest,
                    Revision.expected_issuer,
                    Revision.organization,
                    Revision.application,
                    Revision.client_id,
                ).where(Revision.id == str(revision_id))
            ).one_or_none()
            if revision is None or (revision.integration_id, revision.namespace_id) != (
                str(scope.integration_id),
                str(scope.namespace_id),
            ):
                _fail()
            context = MappingIdentityContext(
                scope.integration_id,
                revision_id,
                scope.namespace_id,
                scope.identity_id,
                scope.account_id,
                revision.config_digest,
                revision.expected_issuer,
                revision.organization,
                revision.application,
                revision.client_id,
                scope.subject,
            )
            reviewed_target = DesiredWorkspaceTarget(
                scope.workspace_id, role.value, role.value, CasdoorDecisionReason(reason), ()
            )
            allocated = CasdoorGenerationRepository(self.session).allocate(
                DesiredWorkspacePlan(context, (reviewed_target,)),
                expected_fence_epoch=scope.fence_epoch,
                expected_generation=0,
            )
            generation = allocated.generation
        prior = MembershipObservation(
            scope.workspace_id, scope.account_id, scope.join_id, scope.current_role, MembershipBackend.LOCAL
        )
        baseline = selected[0]["baseline_json"] if selected else role_baseline_json(prior)
        role_changed = CasdoorLocalRoleRepository(self.session).apply_lifecycle_join_role(
            account_id=scope.account_id,
            workspace_id=scope.workspace_id,
            join_id=scope.join_id,
            expected_role=scope.current_role,
            target_role=role,
        )
        after = MembershipObservation(scope.workspace_id, scope.account_id, scope.join_id, role, MembershipBackend.LOCAL)
        desired = canonical(
            {
                "schema_version": 1,
                "backend": "local",
                "target_role": role.value,
                "builtin_id": role.value,
                "role_ids": [role.value],
                "reason": reason,
                "fence_epoch": scope.fence_epoch,
            }
        )
        epoch = selected[0]["ownership_epoch"] + 1 if selected else 1
        values = dict(
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=epoch,
            source=CasdoorMembershipSource.ADOPT,
            revision_id=str(revision_id),
            desired_generation=generation,
            last_applied_roles_json=role_baseline_json(after),
            last_applied_fingerprint=roles_fingerprint(after),
            desired_roles_json=desired,
            baseline_json=baseline,
            finalization=CasdoorFinalizationState.FINALIZED,
        )
        if selected:
            row = selected[0]
            if reassignment is not None:
                values["identity_id"] = str(scope.identity_id)
            exact_before = tuple(
                getattr(History, key) == value for key, value in reassignment["before"].items()
            ) if reassignment is not None else ()
            result = self.session.execute(
                sa.update(History)
                .where(
                    History.id == row["id"],
                    History.ownership == row["ownership"],
                    History.ownership_epoch == row["ownership_epoch"],
                    History.join_id == str(scope.join_id),
                    History.desired_generation == row["desired_generation"],
                    *exact_before,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                _fail()
            membership_id = row["id"]
        else:
            membership_id = str(uuid4())
            self.session.add(
                History(
                    id=membership_id,
                    namespace_id=str(scope.namespace_id),
                    identity_id=str(scope.identity_id),
                    account_id=str(scope.account_id),
                    workspace_id=str(scope.workspace_id),
                    join_id=str(scope.join_id),
                    **values,
                )
            )
        self.session.flush()
        if role_changed:
            InvitationAuthorityRepository().record_local_role_change(
                self.session, account_id=str(scope.account_id), workspace_id=str(scope.workspace_id)
            )
        self.session.add(
            Audit(
                id=adoption_audit_id,
                namespace_id=str(scope.namespace_id),
                revision_id=str(revision_id),
                identity_id=str(scope.identity_id),
                account_id=str(scope.account_id),
                actor_account_id=str(actor_account_id),
                action="local_membership_adopt_v1",
                result_code="success",
                correlation_id=str(correlation_id),
                summary_json=canonical(
                    {
                        "schema_version": 1,
                        "membership_id": membership_id,
                        "ownership_epoch": epoch,
                        "workspace_id": str(scope.workspace_id),
                        "join_id": str(scope.join_id),
                        "review_sha256": scope.fingerprint,
                        "baseline_sha256": digest(baseline),
                        "target_role": role.value,
                    }
                ),
            )
        )
        self.session.flush()
        self.final_join(scope, expected_role=role)
        if reassignment is not None:
            adoption_rows = self._archived_rows(Audit, Audit.id == adoption_audit_id, lock=True)
            if generation != 1 or len(adoption_rows) != 1:
                _fail()
            summary = self._reassignment_summary(reassignment, scope=scope, role=role,
                adoption_audit_id=adoption_audit_id,
                adoption_sha256=digest(self._archived_encode(dict(adoption_rows[0]))))
            if len(summary.encode()) > MAX_TEXT:
                _fail()
            self.session.add(Audit(
                namespace_id=str(scope.namespace_id), revision_id=str(revision_id), identity_id=str(scope.identity_id),
                account_id=str(scope.account_id), actor_account_id=str(actor_account_id),
                action="local_membership_reassignment_v1", result_code="success",
                correlation_id=str(correlation_id), summary_json=summary,
            ))
            self.session.flush()
            self._retired_responsibility(account_id=scope.account_id, namespace_id=scope.namespace_id,
                selected_identity=None, lock=True, force=True)
        return {"membership_id": membership_id, "ownership_epoch": epoch}

    def final_join(self, scope, *, expected_role=None):
        actual = self.session.execute(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.role)
            .where(
                TenantAccountJoin.account_id == str(scope.account_id),
                TenantAccountJoin.tenant_id == str(scope.workspace_id),
            )
            .with_for_update()
        ).one_or_none()
        if actual is None or tuple(actual) != (str(scope.join_id), expected_role or scope.current_role):
            _fail()

    def require_released_for_unlink(self, account_id, identity_id):
        """Rebuild actual release provenance for every retained historical row.

        This is not a public receipt/summary verifier. Only closed SQL audit rows
        produced with the actual ownership CAS can match the current row bytes.
        All intent kinds/states remain an unconditional barrier in this domain.
        """
        bindings = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=False)
        selected = [r for r in bindings if r["id"] == str(identity_id)]
        if len(selected) != 1:
            _fail()
        archive = self._archived_release_facts(
            account_id=account_id, namespace_id=selected[0]["namespace_id"], identity_id=identity_id,
        )
        discovery = self._rows(
            sa.select(History.workspace_id)
            .where(History.account_id == str(account_id))
            .order_by(History.workspace_id)
            .limit(MAX_ROWS + 1),
            lock=False,
        )
        workspace_ids = sorted({row["workspace_id"] for row in discovery})
        parents = self._rows(
            sa.select(Tenant.id, Tenant.status).where(Tenant.id.in_(workspace_ids)).order_by(Tenant.id), lock=True
        )
        if {row["id"] for row in parents} != set(workspace_ids) or any(
            row["status"] is not TenantStatus.NORMAL for row in parents
        ):
            _fail()
        joins = self._rows(
            sa.select(TenantAccountJoin.id, TenantAccountJoin.tenant_id, TenantAccountJoin.role)
            .where(TenantAccountJoin.account_id == str(account_id), TenantAccountJoin.tenant_id.in_(workspace_ids))
            .order_by(TenantAccountJoin.tenant_id),
            lock=True,
        )
        histories = self._history(account_id, lock=True)
        if archive[0]:
            retained = [r for r in histories if r["namespace_id"] != selected[0]["namespace_id"]]
            if {r["id"] for r in retained} != set(archive[3]):
                _fail()
            histories = tuple(r for r in histories if r["namespace_id"] == selected[0]["namespace_id"])
        if {row["workspace_id"] for row in self._history(account_id, lock=True)} != set(workspace_ids):
            _fail()
        self.require_no_intents(account_id, identity_id, lock=True)
        if not histories:
            return
        if any(
            row["identity_id"] != str(identity_id) or row["ownership"] is not CasdoorMembershipOwnership.RELEASED
            for row in histories
        ):
            _fail()
        audits = self._archived_rows(Audit, sa.and_(
            Audit.account_id == str(account_id), Audit.identity_id == str(identity_id),
            Audit.action == RELEASE_ACTION, Audit.result_code == "success",
        ), lock=True)
        for row in histories:
            matches = []
            for audit in audits:
                try:
                    value = self._archived_summary(audit, {
                        "schema_version", "membership_id", "workspace_id", "join_id", "ownership_epoch",
                        "row_sha256", "review_sha256", "retained_role",
                    })
                    if (
                        value["membership_id"] == row["id"]
                        and value["ownership_epoch"] == row["ownership_epoch"]
                        and value["workspace_id"] == row["workspace_id"]
                        and value["join_id"] == row["join_id"]
                        and value["row_sha256"] == digest(row)
                        and audit["namespace_id"] == row["namespace_id"]
                        and audit["revision_id"] == row["revision_id"]
                        and audit["actor_account_id"] is not None
                    ):
                        matches.append(value)
                except (ValueError, TypeError, KeyError):
                    _fail()
            if len(matches) != 1:
                _fail()
            join = next((value for value in joins if value["tenant_id"] == row["workspace_id"]), None)
            if join is None or (join["id"], join["role"]) != (
                row["join_id"],
                TenantAccountRole(matches[0]["retained_role"]),
            ):
                _fail()

    def _namespace_reset_rows(self, model, predicate, *, lock=False):
        """Reject count/UTF-8 byte overflow before fetching any stored text."""
        columns = tuple(model.__table__.columns)
        text = columns
        sqlite = self.session.get_bind().dialect.name == "sqlite"
        byte_lengths = {
            column.name: sa.func.length(sa.cast(column, sa.LargeBinary))
            if sqlite
            else sa.func.octet_length(sa.cast(column, sa.Text))
            for column in text
        }
        lengths = self._rows(
            sa.select(
                model.id,
                *(byte_lengths[column.name].label("bytes_" + column.name) for column in text),
            )
            .where(predicate)
            .order_by(model.id)
            .limit(MAX_ROWS + 1),
            lock=lock,
        )
        if any(
            row["bytes_" + column.name] is not None and row["bytes_" + column.name] > MAX_TEXT
            for row in lengths
            for column in text
        ):
            _fail()
        discovered_ids = [row["id"] for row in lengths]
        bounded = tuple(sa.or_(column.is_(None), byte_lengths[column.name] <= MAX_TEXT) for column in text)
        rows = self._rows(
            sa.select(*columns)
            .where(predicate, model.id.in_(discovered_ids), *bounded)
            .order_by(model.id)
            .limit(MAX_ROWS + 1),
            lock=lock,
        )
        current_ids = self._rows(sa.select(model.id).where(predicate).order_by(model.id).limit(MAX_ROWS + 1), lock=lock)
        if discovered_ids != [row["id"] for row in rows] or discovered_ids != [row["id"] for row in current_ids]:
            _fail()
        # Internal only: never serialize account credentials, ciphertext or subjects.
        return tuple(
            {
                key: value.isoformat()
                if hasattr(value, "isoformat")
                else str(value)
                if value is not None and not isinstance(value, (str, int, float, bool, dict, list))
                else value
                for key, value in row.items()
            }
            for row in rows
        )

    def inspect_namespace_reset(self, namespace_id, *, source_account_id, lock=False):
        """Closed Casdoor persistent LOCAL domain, not global remote quiescence."""
        from core.casdoor.errors import CasdoorDecisionReason
        from models.casdoor_extend import CasdoorNamespaceLifecycle, CasdoorValidationExtend
        from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
        from repositories.casdoor_identity_lifecycle_repository_extend import CasdoorIdentityLifecycleRepository

        # Original release provenance reader takes locks even during navigation.
        # Hold the entire ordered parent set first in this short root, never a subset.
        lock = True
        with self.session.no_autoflush:
            integrations = self._namespace_reset_rows(Integration, Integration.slot == 1, lock=lock)
            if len(integrations) != 1 or integrations[0]["enabled"] is not False:
                _fail()
            integration = integrations[0]
            namespaces = self._namespace_reset_rows(Namespace, Namespace.integration_id == integration["id"], lock=lock)
            namespace = next((row for row in namespaces if row["id"] == str(namespace_id)), None)
            if (
                namespace is None
                or namespace["lifecycle"] != CasdoorNamespaceLifecycle.FENCING
                or type(namespace["fence_epoch"]) is not int
                or not 0 <= namespace["fence_epoch"] < 9223372036854775807
            ):
                _fail()
            graph_namespaces, graph, graph_facts = self._retained_reset_graph(namespace_id)
            component_ids = sorted({str(namespace_id)} | set(graph))
            # Discovery is scalar-only; full accounts/identities wait for their ordered parents.
            discovered = self._rows(
                sa.select(Identity.account_id, Identity.namespace_id)
                .where(Identity.namespace_id.in_(component_ids))
                .order_by(Identity.id)
                .limit(MAX_ROWS + 1),
                lock=False,
            )
            historical = self._rows(
                sa.select(History.account_id, History.namespace_id)
                .where(History.namespace_id.in_(component_ids))
                .order_by(History.id)
                .limit(MAX_ROWS + 1),
                lock=False,
            )
            account_ids = sorted({str(source_account_id)} | {row["account_id"] for row in (*discovered, *historical)})
            credential_accounts = sorted({str(source_account_id)} | {
                row["account_id"] for row in (*discovered, *historical) if row["namespace_id"] == str(namespace_id)
            })
            if len(account_ids) > MAX_ROWS:
                _fail()
            accounts = self._namespace_reset_rows(Account, Account.id.in_(account_ids), lock=lock)
            if [row["id"] for row in accounts] != account_ids or any(
                row["status"] != AccountStatus.ACTIVE for row in accounts if row["id"] in credential_accounts
            ):
                _fail()
            for account_id in credential_accounts:
                account = self.session.get(Account, account_id, populate_existing=True)
                if not CasdoorIdentityLifecycleRepository.password_available(account):
                    _fail()
            identities = self._namespace_reset_rows(
                Identity,
                sa.or_(Identity.namespace_id.in_(component_ids), Identity.account_id.in_(account_ids)),
                lock=lock,
            )
            by_ns = {r["id"]: r for r in namespaces}
            if any(
                row["namespace_id"] not in by_ns or row["account_id"] not in account_ids
                or (row["issuer"], row["organization"]) != (
                    by_ns[row["namespace_id"]]["expected_issuer"], by_ns[row["namespace_id"]]["organization"])
                or (row["namespace_id"] != str(namespace_id)
                    and by_ns[row["namespace_id"]]["lifecycle"] != CasdoorNamespaceLifecycle.ARCHIVED)
                for row in identities
            ):
                _fail()
            if len({(row["account_id"], row["namespace_id"]) for row in identities}) != len(identities):
                _fail()
            revision_rows = self._namespace_reset_rows(Revision, Revision.namespace_id.in_(graph_namespaces), lock=False)
            revisions = {row["id"]: row for row in revision_rows}
            pointer = integration["draft_revision_id"] or integration["active_revision_id"]
            if pointer is None or any(
                value is not None and value not in revisions
                for value in (integration["draft_revision_id"], integration["active_revision_id"])
            ):
                _fail()
            # Only read/validate immutable revision owner; no crypto operation here.
            reader = CasdoorConfigurationRepository(self.session, crypto=None, rbac_enabled=False)
            configurations = [
                reader._configuration(reader._revision(integration["id"], row["id"])) for row in revision_rows
            ]
            validations = self._namespace_reset_rows(
                CasdoorValidationExtend, CasdoorValidationExtend.revision_id.in_(revisions), lock=False
            )
            if any(
                row["rbac_mode"] not in (None, "off")
                or row["config_digest"] != revisions[row["revision_id"]]["config_digest"]
                for row in validations
            ):
                _fail()
            history_navigation = self._rows(
                sa.select(History.id, History.workspace_id)
                .where(sa.or_(History.namespace_id.in_(component_ids), History.account_id.in_(account_ids),
                              History.identity_id.in_([row["id"] for row in identities]),
                              History.join_id.in_(sa.select(TenantAccountJoin.id).where(TenantAccountJoin.account_id.in_(account_ids)))))
                .order_by(History.id)
                .limit(MAX_ROWS + 1),
                lock=False,
            )
            join_navigation = self._rows(
                sa.select(TenantAccountJoin.id, TenantAccountJoin.tenant_id)
                .where(TenantAccountJoin.account_id.in_(account_ids))
                .order_by(TenantAccountJoin.id)
                .limit(MAX_ROWS + 1),
                lock=False,
            )
            workspace_ids = sorted(
                {row["workspace_id"] for row in history_navigation}
                | {row["tenant_id"] for row in join_navigation}
                | {str(config.default_workspace_id) for config in configurations}
                | {str(mapping.workspace_id) for config in configurations for mapping in config.workspace_mappings}
            )
            if len(workspace_ids) > MAX_ROWS:
                _fail()
            parents = self._namespace_reset_rows(Tenant, Tenant.id.in_(workspace_ids), lock=lock)
            if [row["id"] for row in parents] != workspace_ids or any(
                row["status"] != TenantStatus.NORMAL for row in parents
            ):
                _fail()
            joins = self._namespace_reset_rows(
                TenantAccountJoin, TenantAccountJoin.account_id.in_(account_ids), lock=lock
            )
            if [row["id"] for row in joins] != [row["id"] for row in join_navigation]:
                _fail()
            histories = self._namespace_reset_rows(
                History,
                sa.or_(History.namespace_id.in_(component_ids), History.account_id.in_(account_ids),
                       History.identity_id.in_([row["id"] for row in identities]),
                       History.join_id.in_([row["id"] for row in joins])),
                lock=lock,
            )
            if [row["id"] for row in histories] != [row["id"] for row in history_navigation]:
                _fail()
            if any(row["account_id"] not in account_ids or row["namespace_id"] not in component_ids for row in histories):
                _fail()
            for account_id in account_ids:
                all_owned = self._history(UUID(account_id), lock=lock)
                archive = self._archived_reset_account_facts(
                    account_id=account_id, namespace_id=namespace_id,
                )
                old_owned = [r for r in all_owned if r["namespace_id"] != str(namespace_id)]
                if {r["id"] for r in old_owned} != set(archive[3]):
                    _fail()
                owned = tuple(r for r in all_owned if r["namespace_id"] == str(namespace_id))
                if any(r["revision_id"] not in revisions for r in owned):
                    _fail()
                current = [r for r in identities if r["account_id"] == account_id and r["namespace_id"] == str(namespace_id)]
                retired, _records, _retired_facts = self._retired_responsibility(
                    account_id=account_id, namespace_id=namespace_id,
                    selected_identity=current[0] if current else None, lock=lock,
                )
                if len(current) > 1 or (owned and not current and any(r["id"] not in retired for r in owned)):
                    _fail()
                if current:
                    self.require_released_for_unlink(UUID(account_id), UUID(current[0]["id"]))
                for row in owned:
                    try:
                        desired = json.loads(row["desired_roles_json"])
                        role = desired["target_role"]
                        if (
                            set(desired)
                            != {
                                "schema_version",
                                "backend",
                                "target_role",
                                "builtin_id",
                                "role_ids",
                                "reason",
                                "fence_epoch",
                            }
                            or desired["schema_version"] != 1
                            or type(desired["schema_version"]) is not int
                        ):
                            _fail()
                        if (
                            desired["backend"] != "local"
                            or role not in ("admin", "editor", "normal")
                            or desired["builtin_id"] != role
                            or desired["role_ids"] != [role]
                            or type(desired["reason"]) is not str
                            or desired["reason"] not in {reason.value for reason in CasdoorDecisionReason}
                        ):
                            _fail()
                        if (
                            type(desired["fence_epoch"]) is not int
                            or not 0 <= desired["fence_epoch"] < namespace["fence_epoch"]
                            or canonical(desired) != row["desired_roles_json"]
                        ):
                            _fail()
                    except (ValueError, TypeError, KeyError):
                        _fail()
            predicate = sa.or_(
                Intent.namespace_id.in_(component_ids),
                Intent.account_id.in_(account_ids),
                Intent.identity_id.in_([row["id"] for row in identities] + [row["identity_id"] for row in histories]),
                Intent.membership_id.in_([row["id"] for row in histories]),
            )
            if self._rows(sa.select(Intent.id).where(predicate).limit(1), lock=lock):
                _fail()
            audits = self._namespace_reset_rows(
                Audit, sa.or_(Audit.namespace_id.in_(graph_namespaces), Audit.account_id.in_(account_ids)), lock=lock
            )
            allowed_actions = {
                "draft_save",
                "draft_clear_secret",
                "static_validate",
                "activate",
                "disable",
                "identity_link",
                "identity_unlink",
                "local_membership_release_v1",
                "local_membership_adopt_v1",
                "local_membership_reassignment_v1",
                "local_member_withdraw",
                "local_member_regrant",
                "start",
                "callback",
                "diagnostic",
                "link",
                "reauth",
                "local_namespace_reset_v1",
                "local_role_apply",
                "profile_sync",
                "local_manual_override",
            }
            if any(row["action"] not in allowed_actions for row in audits):
                _fail()
            preserved = {
                "accounts": accounts,
                "identities": identities,
                "histories": histories,
                "joins": joins,
                "parents": parents,
                "revisions": revision_rows,
                "validations": validations,
                "audits": audits,
            }
            result = {
                "namespace_id": str(namespace_id),
                "integration_id": integration["id"],
                "etag": integration["etag"],
                "fence_epoch": namespace["fence_epoch"],
                "accounts": tuple(account_ids),
                "credential_accounts": tuple(credential_accounts),
                "component_ids": tuple(component_ids),
                "workspace_ids": tuple(workspace_ids),
                "identities": identities,
                "preserved": preserved,
                "pointer": pointer,
                "ancestor_namespaces": tuple(r for r in namespaces if r["id"] != str(namespace_id)),
                "graph_facts": graph_facts,
            }
            result["fingerprint"] = digest(
                {"integration": integration, "namespaces": namespaces, "preserved": preserved}
            )
            return result

    def _namespace_reset_readback(self, scope):
        """Check preserved scope and zero intents after mutation, without new authority."""
        from models.casdoor_extend import CasdoorValidationExtend

        models = {
            "accounts": Account,
            "identities": Identity,
            "histories": History,
            "joins": TenantAccountJoin,
            "parents": Tenant,
            "revisions": Revision,
            "validations": CasdoorValidationExtend,
            "audits": Audit,
        }
        for key, model in models.items():
            if (
                self._namespace_reset_rows(model, model.id.in_([row["id"] for row in scope["preserved"][key]]))
                != scope["preserved"][key]
            ):
                _fail()
        namespace_id, accounts = scope["namespace_id"], scope["accounts"]
        if self._namespace_reset_rows(Namespace, Namespace.id.in_([r["id"] for r in scope["ancestor_namespaces"]])) != scope["ancestor_namespaces"]:
            _fail()
        identity_rows = self._namespace_reset_rows(
            Identity, sa.or_(Identity.namespace_id.in_(scope["component_ids"]), Identity.account_id.in_(accounts))
        )
        history_rows = self._namespace_reset_rows(
            History, sa.or_(History.namespace_id.in_(scope["component_ids"]), History.account_id.in_(accounts),
                            History.identity_id.in_([row["id"] for row in identity_rows]),
                            History.join_id.in_(sa.select(TenantAccountJoin.id).where(TenantAccountJoin.account_id.in_(accounts))))
        )
        joins = self._namespace_reset_rows(TenantAccountJoin, TenantAccountJoin.account_id.in_(accounts))
        audits = self._namespace_reset_rows(
            Audit,
            sa.and_(
                sa.or_(Audit.namespace_id.in_([namespace_id] + [r["id"] for r in scope["ancestor_namespaces"]]), Audit.account_id.in_(accounts)),
                sa.not_(sa.and_(Audit.namespace_id == namespace_id, Audit.action == "local_namespace_reset_v1")),
            ),
        )
        if audits != scope["preserved"]["audits"]:
            _fail()
        integration = self._archived_rows(Integration, Integration.id == scope["integration_id"])
        if len(integration) != 1:
            _fail()
        revisions = self._archived_rows(Revision, Revision.id == integration[0]["draft_revision_id"],
            columns=tuple(c for c in Revision.__table__.columns if c.name != "encrypted_secret"))
        if len(revisions) != 1:
            _fail()
        ledger = self._archived_rows(Audit, sa.and_(Audit.namespace_id == namespace_id, Audit.action == "local_namespace_reset_v1"))
        if len(ledger) != 1:
            _fail()
        value = self._archived_summary(ledger[0], {"schema_version", "old_namespace_id", "new_namespace_id", "new_revision_id", "review_sha256", "fence_epoch", "etag"})
        if (value["new_namespace_id"], value["new_revision_id"], value["review_sha256"], value["fence_epoch"], value["etag"]) != (
            revisions[0]["namespace_id"], revisions[0]["id"], scope["fingerprint"], scope["fence_epoch"] + 1, scope["etag"] + 1):
            _fail()
        namespaces, _edges, _facts = self._retained_reset_graph(revisions[0]["namespace_id"])
        if set(namespaces) != {r["id"] for r in scope["ancestor_namespaces"]} | {namespace_id, revisions[0]["namespace_id"]}:
            _fail()
        if (
            identity_rows != scope["preserved"]["identities"]
            or history_rows != scope["preserved"]["histories"]
            or joins != scope["preserved"]["joins"]
        ):
            _fail()
        if self._rows(
            sa.select(Intent.id)
            .where(
                sa.or_(
                    Intent.namespace_id.in_(scope["component_ids"]),
                    Intent.account_id.in_(accounts),
                    Intent.identity_id.in_([row["id"] for row in identity_rows] + [row["identity_id"] for row in history_rows]),
                    Intent.membership_id.in_([row["id"] for row in history_rows]),
                )
            )
            .limit(1),
            lock=True,
        ):
            _fail()
        from repositories.casdoor_identity_lifecycle_repository_extend import CasdoorIdentityLifecycleRepository

        for account_id in scope["credential_accounts"]:
            if not CasdoorIdentityLifecycleRepository.password_available(
                self.session.get(Account, account_id, populate_existing=True)
            ):
                _fail()

    def _archived_rows(self, model, predicate, *, lock=False, columns=None):
        """Complete bounded SQL facts; reject byte overflow before materialization."""
        columns = tuple(model.__table__.columns) if columns is None else columns
        sqlite = self.session.get_bind().dialect.name == "sqlite"
        sizes = {
            c.name: sa.func.length(sa.cast(c, sa.LargeBinary)) if sqlite else sa.func.octet_length(sa.cast(c, sa.Text))
            for c in columns
        }
        navigation = self._rows(
            sa.select(model.id, *(sizes[c.name].label("bytes_" + c.name) for c in columns))
            .where(predicate)
            .order_by(model.id)
            .limit(MAX_ROWS + 1),
            lock=lock,
        )
        if any(
            row["bytes_" + c.name] is not None and row["bytes_" + c.name] > MAX_TEXT for row in navigation for c in columns
        ):
            _fail()
        ids = tuple(row["id"] for row in navigation)
        guards = tuple(sa.or_(c.is_(None), sizes[c.name] <= MAX_TEXT) for c in columns)
        rows = self._rows(
            sa.select(*columns).where(predicate, model.id.in_(ids), *guards).order_by(model.id).limit(MAX_ROWS + 1),
            lock=lock,
        )
        fresh = self._rows(sa.select(model.id).where(predicate).order_by(model.id).limit(MAX_ROWS + 1), lock=lock)
        if ids != tuple(row["id"] for row in rows) or ids != tuple(row["id"] for row in fresh):
            _fail()
        return rows

    @staticmethod
    def _archived_uuid(value):
        try:
            if type(value) is not str or str(UUID(value)) != value:
                _fail()
        except (ValueError, TypeError, AttributeError):
            _fail()

    @staticmethod
    def _archived_counter(value, *, minimum=0):
        if type(value) is not int or not minimum <= value < 2**63 - 1:
            _fail()

    @staticmethod
    def _archived_hex(value):
        if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            _fail()

    @staticmethod
    def _archived_encode(value):
        def safe(item):
            if hasattr(item, "isoformat"):
                return item.isoformat()
            if isinstance(item, dict):
                return {key: safe(v) for key, v in item.items()}
            if isinstance(item, (tuple, list)):
                return [safe(v) for v in item]
            return item

        return canonical(safe(value))

    def _archived_summary(self, audit, keys):
        try:
            value = json.loads(audit["summary_json"])
            if (
                type(value) is not dict
                or set(value) != keys
                or canonical(value) != audit["summary_json"]
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
            ):
                _fail()
            for key in keys & {"review_sha256", "row_sha256", "baseline_sha256"}:
                self._archived_hex(value[key])
            for key in keys & {
                "membership_id",
                "workspace_id",
                "join_id",
                "old_namespace_id",
                "new_namespace_id",
                "new_revision_id",
            }:
                self._archived_uuid(value[key])
            for key in keys & {"ownership_epoch", "fence_epoch", "etag"}:
                self._archived_counter(value[key], minimum=1)
            for key in keys & {"retained_role", "target_role"}:
                if value[key] not in ("admin", "editor", "normal") or type(value[key]) is not str:
                    _fail()
            for key in ("id", "namespace_id", "revision_id", "actor_account_id", "correlation_id"):
                self._archived_uuid(audit[key])
            if audit["result_code"] != "success" or not isinstance(audit["created_at"], datetime):
                _fail()
            for key in ("account_id", "identity_id"):
                if audit[key] is not None:
                    self._archived_uuid(audit[key])
            return value
        except (ValueError, TypeError, KeyError):
            _fail()

    def _archived_release_facts(self, *, account_id, namespace_id, identity_id, lock=False):
        """Current caller requires its exact real binding before historical observation."""
        identities = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=lock)
        selected = [r for r in identities if (r["id"], r["namespace_id"]) == (str(identity_id), str(namespace_id))]
        if len(selected) != 1:
            _fail()
        return self._archived_account_release_facts(
            account_id=account_id, namespace_id=namespace_id, selected_identity=selected[0], lock=lock,
        )

    def _retained_reset_graph(self, namespace_id, *, lock=False):
        """Bounded original ledger component, including namespaces without bindings.

        This reader observes historical SQL facts only. It cannot grant current
        admission, adoption or mutation authority, and never reads ciphertext.
        """
        target_rows = self._archived_rows(Namespace, Namespace.id == str(namespace_id), lock=lock)
        if len(target_rows) != 1:
            _fail()
        target = target_rows[0]
        integrations = self._archived_rows(Integration, Integration.slot == 1, lock=lock)
        if len(integrations) != 1 or integrations[0]["id"] != target["integration_id"]:
            _fail()
        if target["lifecycle"] not in (CasdoorNamespaceLifecycle.ACTIVE, CasdoorNamespaceLifecycle.FENCING):
            _fail()
        namespaces = self._archived_rows(Namespace, Namespace.integration_id == target["integration_id"], lock=lock)
        by_ns = {r["id"]: r for r in namespaces}
        for row in namespaces:
            for key in ("id", "integration_id"):
                self._archived_uuid(row[key])
            self._archived_hex(row["core_fingerprint"])
            self._archived_counter(row["fence_epoch"])
            if not isinstance(row["created_at"], datetime):
                _fail()
        # The singleton integration owns this domain. Foreign reset audit rows
        # cannot silently form an unobserved incoming edge to its component.
        audits = self._archived_rows(Audit, Audit.action == "local_namespace_reset_v1", lock=lock)
        edges, incoming, facts = {}, {}, []
        public_columns = tuple(c for c in Revision.__table__.columns if c.name != "encrypted_secret")
        from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

        owner = CasdoorConfigurationRepository(self.session, crypto=None, rbac_enabled=False)
        for audit in audits:
            value = self._archived_summary(audit, {
                "schema_version", "old_namespace_id", "new_namespace_id", "new_revision_id",
                "review_sha256", "fence_epoch", "etag",
            })
            old_id, new_id = value["old_namespace_id"], value["new_namespace_id"]
            old, new = by_ns.get(old_id), by_ns.get(new_id)
            if (
                old is None or new is None or old_id == new_id
                or old_id in edges or new_id in incoming
                or audit["namespace_id"] != old_id
                or audit["account_id"] is not None or audit["identity_id"] is not None
                or old["lifecycle"] is not CasdoorNamespaceLifecycle.ARCHIVED
                or not isinstance(old["archived_at"], datetime)
                or old["fence_epoch"] != value["fence_epoch"]
            ):
                _fail()
            revisions = self._archived_rows(
                Revision, Revision.id.in_([audit["revision_id"], value["new_revision_id"]]),
                lock=lock, columns=public_columns,
            )
            refs = {r["id"]: r for r in revisions}
            if (
                set(refs) != {audit["revision_id"], value["new_revision_id"]}
                or refs[audit["revision_id"]]["namespace_id"] != old_id
                or refs[value["new_revision_id"]]["namespace_id"] != new_id
            ):
                _fail()
            for revision in revisions:
                for key in ("id", "integration_id", "namespace_id", "default_workspace_id"):
                    self._archived_uuid(revision[key])
                self._archived_counter(revision["revision_number"], minimum=1)
                self._archived_hex(revision["config_digest"])
                ns = by_ns[revision["namespace_id"]]
                config = owner._configuration(SimpleNamespace(**dict(revision)))
                if (
                    revision["integration_id"] != target["integration_id"]
                    or owner._core_fingerprint(config) != ns["core_fingerprint"]
                    or any(getattr(config, k) != ns[k] for k in ("expected_issuer", "organization", "application", "client_id"))
                ):
                    _fail()
            validations = self._archived_rows(Validation, Validation.revision_id.in_(refs), lock=lock)
            for row in validations:
                self._archived_uuid(row["id"])
                self._archived_uuid(row["revision_id"])
                self._archived_hex(row["config_digest"])
                if row["rbac_mode"] != "off" or row["config_digest"] != refs[row["revision_id"]]["config_digest"]:
                    _fail()
            edges[old_id] = (new_id, audit, value)
            incoming[new_id] = old_id
            facts.extend((self._archived_encode(dict(old)), self._archived_encode(dict(audit)),
                          self._archived_encode([dict(r) for r in revisions]),
                          self._archived_encode([dict(r) for r in validations])))
        component = set()
        for old_id in edges:
            cursor, seen = old_id, set()
            while cursor != str(namespace_id):
                if cursor in seen or cursor not in edges or len(seen) >= MAX_ROWS:
                    _fail()
                seen.add(cursor)
                cursor = edges[cursor][0]
            component.update(seen)
        # Archived namespace labels alone are never enough, even without Identity.
        if {r["id"] for r in namespaces if r["lifecycle"] is CasdoorNamespaceLifecycle.ARCHIVED} != component:
            _fail()
        # Original unbound configuration saves can retain unused ACTIVE parents.
        # Such metadata is outside this ledger component only while it has no
        # identity, membership or operation responsibility of its own.
        unused = set(by_ns) - component - {str(namespace_id)}
        for model in (Identity, History, Intent):
            if self._rows(sa.select(model.id).where(model.namespace_id.in_(unused))
                          .order_by(model.id).limit(MAX_ROWS + 1), lock=False):
                _fail()
        return by_ns, edges, tuple(sorted(facts))

    def _archived_reset_account_facts(self, *, account_id, namespace_id, lock=False):
        """Reset observation allows an actually absent current binding, never a fake ID."""
        identities = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=lock)
        selected = [r for r in identities if r["namespace_id"] == str(namespace_id)]
        if len(selected) > 1:
            _fail()
        return self._archived_account_release_facts(
            account_id=account_id, namespace_id=namespace_id,
            selected_identity=selected[0] if selected else None, lock=lock,
        )

    def _unlink_parent_union(self, *, account_id, namespace_id, identity_id):
        """Hold the discovered parent union before any identity or Join child lock."""
        integrations = self._archived_rows(Integration, Integration.slot == 1, lock=True)
        if len(integrations) != 1:
            _fail()
        namespaces = self._archived_rows(Namespace, Namespace.integration_id == integrations[0]["id"], lock=True)
        if str(namespace_id) not in {r["id"] for r in namespaces}:
            _fail()
        accounts = self._archived_rows(Account, Account.id == str(account_id), lock=True)
        if len(accounts) != 1 or accounts[0]["status"] is not AccountStatus.ACTIVE:
            _fail()
        identities = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=True)
        if len([r for r in identities if (r["id"],r["namespace_id"]) == (str(identity_id),str(namespace_id))]) != 1:
            _fail()
        navigation = self._rows(sa.select(History.id, History.workspace_id).where(
            sa.or_(History.account_id == str(account_id), History.identity_id.in_([r["id"] for r in identities]))
        ).order_by(History.id).limit(MAX_ROWS + 1), lock=False)
        join_navigation = self._rows(sa.select(TenantAccountJoin.id,TenantAccountJoin.tenant_id).where(
            TenantAccountJoin.account_id == str(account_id)
        ).order_by(TenantAccountJoin.id).limit(MAX_ROWS + 1), lock=False)
        workspace_ids = sorted({r["workspace_id"] for r in navigation} | {r["tenant_id"] for r in join_navigation})
        parents = self._archived_rows(Tenant, Tenant.id.in_(workspace_ids), lock=True)
        if {r["id"] for r in parents} != set(workspace_ids) or any(r["status"] is not TenantStatus.NORMAL for r in parents):
            _fail()
        reread = self._rows(sa.select(History.id, History.workspace_id).where(
            sa.or_(History.account_id == str(account_id), History.identity_id.in_([r["id"] for r in identities]))
        ).order_by(History.id).limit(MAX_ROWS + 1), lock=False)
        if navigation != reread:
            _fail()

    def _archived_account_release_facts(self, *, account_id, namespace_id, selected_identity, lock=False):
        """Historical SQL responsibility only; never require today's Join/role."""
        identities = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=lock)
        identity_id = selected_identity["id"] if selected_identity is not None else None
        if selected_identity is not None and selected_identity not in identities:
            _fail()
        for row in identities:
            for field in ("id", "namespace_id", "account_id"):
                self._archived_uuid(row[field])
            self._archived_counter(row["sync_generation"])
            from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey

            key = VerifiedIdentityKey(UUID(row["namespace_id"]), row["issuer"], row["organization"], row["subject"])
            if row["subject_digest"] != key.subject_digest:
                _fail()
        old = [r for r in identities if r["id"] != str(identity_id)]
        if not old:
            retired, _records, retired_facts = self._retired_responsibility(
                account_id=account_id, namespace_id=namespace_id, selected_identity=selected_identity, lock=lock,
            )
            if not retired_facts:
                return (), (), (), ()
            self._retained_reset_graph(namespace_id, lock=lock)
            return retired_facts, (), tuple(sorted({r["before"]["workspace_id"] for r in retired.values()})), tuple(sorted(
                key for key, r in retired.items() if r["before"]["namespace_id"] != str(namespace_id)
            ))
        # Preserve the original generic ambiguity rejection before querying
        # archive-only producer tables. An ACTIVE old binding is not an archive.
        preliminary = self._archived_rows(Namespace, Namespace.id.in_([r["namespace_id"] for r in identities]), lock=lock)
        preliminary_ns = {r["id"]: r for r in preliminary}
        target = preliminary_ns.get(str(namespace_id))
        if target is None:
            # A reset may have no current Identity; its real target parent is
            # still mandatory and comes from SQL, never a synthesized binding.
            targets = self._archived_rows(Namespace, Namespace.id == str(namespace_id), lock=lock)
            if len(targets) != 1:
                _fail()
            target = targets[0]
        if target["lifecycle"] not in (CasdoorNamespaceLifecycle.ACTIVE, CasdoorNamespaceLifecycle.FENCING) or any(
            r["namespace_id"] not in preliminary_ns
            or preliminary_ns[r["namespace_id"]]["lifecycle"] is not CasdoorNamespaceLifecycle.ARCHIVED
            or preliminary_ns[r["namespace_id"]]["integration_id"] != target["integration_id"]
            for r in old
        ):
            _fail()
        by_ns, graph, graph_facts = self._retained_reset_graph(namespace_id, lock=lock)
        active = by_ns.get(str(namespace_id))
        if (
            active is None
            or active["lifecycle"] not in (CasdoorNamespaceLifecycle.ACTIVE, CasdoorNamespaceLifecycle.FENCING)
            or any(r["namespace_id"] not in by_ns for r in identities)
        ):
            _fail()
        if any(
            (r["issuer"], r["organization"])
            != (by_ns[r["namespace_id"]]["expected_issuer"], by_ns[r["namespace_id"]]["organization"])
            for r in identities
        ):
            _fail()
        old_ids = {r["id"] for r in old}
        old_ns = {r["namespace_id"] for r in old}
        if str(namespace_id) in old_ns or len(old_ns) != len(old):
            _fail()
        if any(
            by_ns[value]["lifecycle"] is not CasdoorNamespaceLifecycle.ARCHIVED
            or not isinstance(by_ns[value]["archived_at"], datetime)
            or by_ns[value]["integration_id"] != active["integration_id"]
            for value in old_ns
        ):
            _fail()
        histories = self._archived_rows(
            History,
            sa.or_(
                History.account_id == str(account_id),
                History.identity_id.in_([r["id"] for r in identities]),
                History.join_id.in_(sa.select(TenantAccountJoin.id).where(TenantAccountJoin.account_id == str(account_id))),
            ),
            lock=lock,
        )
        if any(row["account_id"] != str(account_id) for row in histories):
            _fail()
        retired, _records, retired_facts = self._retired_responsibility(
            account_id=account_id, namespace_id=namespace_id, selected_identity=selected_identity, lock=lock,
        )
        histories = tuple(r for r in histories if r["id"] not in retired)
        # ALL kinds/states/association forms remain barriers in the archived branch.
        intents = self._rows(
            sa.select(Intent.id)
            .where(
                sa.or_(
                    Intent.account_id == str(account_id),
                    Intent.identity_id.in_([r["id"] for r in identities]),
                    Intent.membership_id.in_([r["id"] for r in histories]),
                    Intent.namespace_id.in_(old_ns),
                )
            )
            .limit(MAX_ROWS + 1),
            lock=lock,
        )
        if intents:
            _fail()
        audits = self._archived_rows(
            Audit,
            sa.or_(
                Audit.account_id == str(account_id),
                Audit.identity_id.in_([r["id"] for r in identities]),
                sa.and_(Audit.namespace_id.in_(old_ns), Audit.action == "local_namespace_reset_v1"),
            ),
            lock=lock,
        )
        allowed_actions = {
            "identity_link",
            "identity_unlink",
            "local_membership_release_v1",
            "local_membership_adopt_v1",
            "local_membership_reassignment_v1",
            "local_namespace_reset_v1",
            "local_member_withdraw",
            "local_member_regrant",
            "start",
            "callback",
            "diagnostic",
            "link",
            "reauth",
            "local_role_apply",
            "profile_sync",
            "local_manual_override",
        }
        if any(a["action"] not in allowed_actions for a in audits):
            _fail()
        if any(a["action"] == "local_membership_reassignment_v1"
               and self._archived_encode(dict(a)) not in retired_facts for a in audits):
            _retired, _records, retired_facts = self._retired_responsibility(
                account_id=account_id, namespace_id=namespace_id, selected_identity=selected_identity, lock=lock, force=True,
            )
        facts = list(graph_facts) + list(retired_facts)
        subjects = []
        workspaces = {r["before"]["workspace_id"] for r in retired.values()}
        archived_ids = [key for key, r in retired.items() if r["before"]["namespace_id"] != str(namespace_id)]
        fields = (
            "id",
            "namespace_id",
            "identity_id",
            "account_id",
            "workspace_id",
            "join_id",
            "ownership",
            "ownership_epoch",
            "source",
            "desired_generation",
            "revision_id",
            "last_applied_roles_json",
            "last_applied_fingerprint",
            "desired_roles_json",
            "baseline_json",
            "finalization",
            "tombstone",
        )
        for identity in old:
            ns = by_ns[identity["namespace_id"]]
            if (
                ns["lifecycle"] is not CasdoorNamespaceLifecycle.ARCHIVED
                or not isinstance(ns["archived_at"], datetime)
                or ns["integration_id"] != active["integration_id"]
            ):
                _fail()
            ledgers = [a for a in audits if a["namespace_id"] == ns["id"] and a["action"] == "local_namespace_reset_v1"]
            if len(ledgers) != 1:
                _fail()
            ledger = ledgers[0]
            value = self._archived_summary(
                ledger,
                {
                    "schema_version",
                    "old_namespace_id",
                    "new_namespace_id",
                    "new_revision_id",
                    "review_sha256",
                    "fence_epoch",
                    "etag",
                },
            )
            if (
                (value["old_namespace_id"], value["new_namespace_id"], value["fence_epoch"])
                != (ns["id"], graph[ns["id"]][0], ns["fence_epoch"])
                or type(value["fence_epoch"]) is not int
                or type(value["etag"]) is not int
                or value["etag"] < 1
            ):
                _fail()
            if ledger["account_id"] is not None or ledger["identity_id"] is not None:
                _fail()
            revision_ids = {value["new_revision_id"], ledger["revision_id"]} | {
                r["revision_id"] for r in histories if r["namespace_id"] == ns["id"]
            }
            # Only public revision owner columns, never ciphertext or credentials.
            public_columns = tuple(c for c in Revision.__table__.columns if c.name != "encrypted_secret")
            revisions = self._archived_rows(Revision, Revision.id.in_(revision_ids), lock=lock, columns=public_columns)
            revision_map = {r["id"]: r for r in revisions}
            if set(revision_map) != revision_ids or any(
                r["integration_id"] != active["integration_id"]
                or type(r["config_digest"]) is not str
                or len(r["config_digest"]) != 64
                for r in revisions
            ):
                _fail()
            from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

            configuration_owner = CasdoorConfigurationRepository(self.session, crypto=None, rbac_enabled=False)
            for revision in revisions:
                for field in ("id", "integration_id", "namespace_id", "default_workspace_id"):
                    self._archived_uuid(revision[field])
                self._archived_counter(revision["revision_number"], minimum=1)
                self._archived_hex(revision["config_digest"])
                config = configuration_owner._configuration(SimpleNamespace(**dict(revision)))
                owner_ns = by_ns.get(revision["namespace_id"])
                if (
                    owner_ns is None
                    or configuration_owner._core_fingerprint(config) != owner_ns["core_fingerprint"]
                    or any(
                        getattr(config, f) != owner_ns[f]
                        for f in ("expected_issuer", "organization", "application", "client_id")
                    )
                ):
                    _fail()
            validation_modes = self._archived_rows(Validation, Validation.revision_id.in_(revision_ids), lock=lock)
            for validation in validation_modes:
                self._archived_uuid(validation["id"])
                self._archived_uuid(validation["revision_id"])
                self._archived_hex(validation["config_digest"])
                if (
                    validation["rbac_mode"] != "off"
                    or validation["config_digest"] != revision_map[validation["revision_id"]]["config_digest"]
                ):
                    _fail()
            if (
                revision_map[value["new_revision_id"]]["namespace_id"] != graph[ns["id"]][0]
                or revision_map[ledger["revision_id"]]["namespace_id"] != ns["id"]
            ):
                _fail()
            facts.append(self._archived_encode([dict(r) for r in validation_modes]))
            facts.extend(
                (
                    self._archived_encode(dict(ns)),
                    self._archived_encode(dict(identity)),
                    self._archived_encode(dict(ledger)),
                    self._archived_encode([dict(r) for r in revisions]),
                )
            )
            subjects.append((ns["id"], identity["subject"]))
            owned = [r for r in histories if r["identity_id"] == identity["id"]]
            for raw in owned:
                row = {k: raw[k] for k in fields}
                for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "join_id", "revision_id"):
                    self._archived_uuid(row[field])
                self._archived_counter(row["ownership_epoch"], minimum=1)
                self._archived_counter(row["desired_generation"])
                if (
                    row["namespace_id"] != ns["id"]
                    or revision_map[row["revision_id"]]["namespace_id"] != ns["id"]
                    or row["ownership"] is not CasdoorMembershipOwnership.RELEASED
                    or row["tombstone"] is not False
                    or row["finalization"] is not CasdoorFinalizationState.FINALIZED
                    or not isinstance(row["source"], CasdoorMembershipSource)
                ):
                    _fail()
                try:
                    from core.casdoor.errors import CasdoorDecisionReason

                    baseline = parse_role_baseline_json(row["baseline_json"])
                    applied = parse_role_baseline_json(row["last_applied_roles_json"])
                    desired = json.loads(row["desired_roles_json"])
                    if (
                        set(desired)
                        != {"schema_version", "backend", "target_role", "builtin_id", "role_ids", "reason", "fence_epoch"}
                        or type(desired["schema_version"]) is not int
                        or desired["schema_version"] != 1
                        or desired["target_role"] not in ("admin", "editor", "normal")
                        or desired["builtin_id"] != desired["target_role"]
                        or desired["role_ids"] != [desired["target_role"]]
                    ):
                        _fail()
                    if type(desired["reason"]) is not str or desired["reason"] not in {
                        r.value for r in CasdoorDecisionReason
                    }:
                        _fail()
                    if (
                        baseline.backend is not MembershipBackend.LOCAL
                        or applied.backend is not MembershipBackend.LOCAL
                        or desired["backend"] != "local"
                        or canonical(desired) != row["desired_roles_json"]
                        or type(desired["fence_epoch"]) is not int
                        or not 0 <= desired["fence_epoch"] < ns["fence_epoch"]
                    ):
                        _fail()
                    releases = []
                    for audit in audits:
                        if audit["action"] != RELEASE_ACTION or (
                            audit["namespace_id"],
                            audit["identity_id"],
                            audit["account_id"],
                            audit["revision_id"],
                        ) != (ns["id"], identity["id"], str(account_id), row["revision_id"]):
                            continue
                        receipt = self._archived_summary(
                            audit,
                            {
                                "schema_version",
                                "membership_id",
                                "workspace_id",
                                "join_id",
                                "ownership_epoch",
                                "row_sha256",
                                "review_sha256",
                                "retained_role",
                            },
                        )
                        if receipt["membership_id"] == row["id"] and (
                            receipt["workspace_id"],
                            receipt["join_id"],
                            receipt["ownership_epoch"],
                            receipt["row_sha256"],
                        ) == (row["workspace_id"], row["join_id"], row["ownership_epoch"], digest(row)):
                            if receipt["retained_role"] != applied.join_role.value:
                                _fail()
                            releases.append(audit)
                    if len(releases) != 1:
                        _fail()
                except (ValueError, TypeError, KeyError, AttributeError):
                    _fail()
                facts.extend((canonical(row), self._archived_encode(dict(releases[0]))))
                archived_ids.append(row["id"])
                workspaces.add(row["workspace_id"])
        if any(
            r["identity_id"] not in old_ids | ({str(identity_id)} if identity_id is not None else set())
            or (r["namespace_id"] in old_ns and r["identity_id"] not in old_ids)
            for r in histories
        ):
            _fail()
        return tuple(sorted(facts)), tuple(sorted(subjects)), tuple(sorted(workspaces)), tuple(sorted(archived_ids))

    @staticmethod
    def _retired_history_fields():
        return (
            "id", "namespace_id", "identity_id", "account_id", "workspace_id", "join_id",
            "ownership", "ownership_epoch", "source", "desired_generation", "revision_id",
            "last_applied_roles_json", "last_applied_fingerprint", "desired_roles_json", "baseline_json",
            "finalization", "tombstone",
        )

    def _unlink_action_fact(self, audit, *, action, account_id, identity_id, namespace_id):
        """Original flat binding audit; absence alone never proves deletion."""
        for field in ("id", "namespace_id", "revision_id", "identity_id", "account_id", "actor_account_id", "correlation_id"):
            self._archived_uuid(audit[field])
        if (
            audit["action"] != action or audit["result_code"] != "success" or audit["summary_json"] != "{}"
            or (audit["account_id"], audit["identity_id"], audit["namespace_id"], audit["actor_account_id"])
            != (str(account_id), str(identity_id), str(namespace_id), str(account_id))
            or not isinstance(audit["created_at"], datetime) or audit["created_at"].tzinfo is not None
        ):
            _fail()
        return dict(audit)

    def _unlink_revision_facts(self, namespace_id, revision_ids, *, lock):
        """Read immutable public owner facts, never ciphertext or a new grant."""
        namespaces = self._archived_rows(Namespace, Namespace.id == str(namespace_id), lock=lock)
        if len(namespaces) != 1:
            _fail()
        ns = namespaces[0]
        if ns["lifecycle"] not in (CasdoorNamespaceLifecycle.ACTIVE, CasdoorNamespaceLifecycle.FENCING, CasdoorNamespaceLifecycle.ARCHIVED):
            _fail()
        columns = tuple(c for c in Revision.__table__.columns if c.name != "encrypted_secret")
        revisions = self._archived_rows(Revision, Revision.id.in_(revision_ids), columns=columns, lock=lock)
        if {row["id"] for row in revisions} != set(revision_ids):
            _fail()
        from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository

        owner = CasdoorConfigurationRepository(self.session, crypto=None, rbac_enabled=False)
        for row in revisions:
            self._archived_uuid(row["id"])
            self._archived_hex(row["config_digest"])
            config = owner._configuration(SimpleNamespace(**dict(row)))
            if (
                row["namespace_id"] != str(namespace_id) or row["integration_id"] != ns["integration_id"]
                or owner._core_fingerprint(config) != ns["core_fingerprint"]
                or any(getattr(config, field) != ns[field] for field in ("expected_issuer", "organization", "application", "client_id"))
            ):
                _fail()
        validations = self._archived_rows(Validation, Validation.revision_id.in_(revision_ids), lock=lock)
        revision_map = {r["id"]: r for r in revisions}
        if any(r["rbac_mode"] != "off" or r["config_digest"] != revision_map[r["revision_id"]]["config_digest"] for r in validations):
            _fail()
        return self._archived_encode([dict(ns), [dict(r) for r in revisions], [dict(r) for r in validations]])

    def _released_unlink_proof(self, before, *, account_id, lock):
        """Exact retained LOCAL row, original release and unique actual unlink."""
        fields = self._retired_history_fields()
        if set(before) != {c.name for c in History.__table__.columns}:
            _fail()
        for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "join_id", "revision_id"):
            self._archived_uuid(before[field])
        self._archived_counter(before["ownership_epoch"], minimum=1)
        self._archived_counter(before["desired_generation"])
        if (
            before["account_id"] != str(account_id) or before["ownership"] != CasdoorMembershipOwnership.RELEASED
            or before["finalization"] != CasdoorFinalizationState.FINALIZED or before["tombstone"] is not False
            or before["source"] not in tuple(CasdoorMembershipSource)
            or any(not isinstance(before[k], datetime) or before[k].tzinfo is not None for k in ("created_at", "updated_at"))
        ):
            _fail()
        try:
            from core.casdoor.errors import CasdoorDecisionReason

            baseline = parse_role_baseline_json(before["baseline_json"])
            applied = parse_role_baseline_json(before["last_applied_roles_json"])
            desired = json.loads(before["desired_roles_json"])
            if (
                baseline.backend is not MembershipBackend.LOCAL or applied.backend is not MembershipBackend.LOCAL
                or before["last_applied_fingerprint"] != roles_fingerprint(applied)
                or set(desired) != {"schema_version", "backend", "target_role", "builtin_id", "role_ids", "reason", "fence_epoch"}
                or type(desired["schema_version"]) is not int or desired["schema_version"] != 1
                or desired["backend"] != "local" or desired["target_role"] not in ("admin", "editor", "normal")
                or desired["builtin_id"] != desired["target_role"] or desired["role_ids"] != [desired["target_role"]]
                or desired["reason"] not in {r.value for r in CasdoorDecisionReason}
                or canonical(desired) != before["desired_roles_json"]
            ):
                _fail()
            self._archived_counter(desired["fence_epoch"])
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail()
        # A reused physical Identity ID is not the deleted binding, even if its
        # present namespace/account happen to match the old audit tuple.
        if self._rows(sa.select(Identity.id).where(Identity.id == before["identity_id"]).limit(1), lock=lock):
            _fail()
        audits = self._archived_rows(Audit, sa.and_(Audit.identity_id == before["identity_id"],
            Audit.action.in_(("identity_unlink", RELEASE_ACTION))), lock=lock)
        unlinks = [a for a in audits if a["action"] == "identity_unlink"]
        if len(unlinks) != 1:
            _fail()
        unlink = self._unlink_action_fact(unlinks[0], action="identity_unlink", account_id=account_id,
                                         identity_id=before["identity_id"], namespace_id=before["namespace_id"])
        matches = []
        for audit in audits:
            if audit["action"] != RELEASE_ACTION:
                continue
            value = self._archived_summary(audit, {
                "schema_version", "membership_id", "workspace_id", "join_id", "ownership_epoch",
                "row_sha256", "review_sha256", "retained_role",
            })
            if (audit["account_id"], audit["namespace_id"], audit["identity_id"]) != (
                str(account_id), before["namespace_id"], before["identity_id"]
            ) or audit["created_at"] > unlink["created_at"]:
                _fail()
            if (value["membership_id"], value["ownership_epoch"]) == (before["id"], before["ownership_epoch"]):
                if (
                    value["row_sha256"] != digest({k: before[k] for k in fields})
                    or (value["workspace_id"], value["join_id"], audit["revision_id"], value["retained_role"])
                    != (before["workspace_id"], before["join_id"], before["revision_id"], applied.join_role.value)
                    or not before["created_at"] <= audit["created_at"] <= unlink["created_at"]
                    or before["updated_at"] > unlink["created_at"]
                ):
                    _fail()
                matches.append(dict(audit))
        if len(matches) != 1:
            _fail()
        if self._rows(sa.select(Intent.id).where(sa.or_(
            Intent.account_id == str(account_id), Intent.identity_id == before["identity_id"],
            Intent.membership_id == before["id"], Intent.namespace_id == before["namespace_id"],
        )).order_by(Intent.id).limit(MAX_ROWS + 1), lock=lock):
            _fail()
        revision_fact = self._unlink_revision_facts(before["namespace_id"], {before["revision_id"], unlink["revision_id"]}, lock=lock)
        return {"before": dict(before), "release": matches[0], "unlink": unlink, "revision_fact": revision_fact}

    def _reassignment_summary(self, proof, *, scope, role, adoption_audit_id, adoption_sha256):
        """Private complete initial responsibility fact; never an admission DTO."""
        before_json = self._archived_encode(proof["before"])
        links_json = self._archived_encode(proof["links"])
        return canonical({
            "schema_version": 1, "membership_id": proof["before"]["id"], "before_history_json": before_json,
            "before_sha256": digest(before_json), "release_sha256": digest(self._archived_encode(proof["release"])),
            "unlink_sha256": digest(self._archived_encode(proof["unlink"])),
            "links_json": links_json, "links_sha256": digest(links_json), "new_identity_id": str(scope.identity_id),
            "new_identity_created_at": proof["new_identity_created_at"].isoformat(),
            "initial_generation": 1, "ownership_epoch": proof["before"]["ownership_epoch"] + 1,
            "join_id": str(scope.join_id), "workspace_id": str(scope.workspace_id),
            "baseline_sha256": digest(proof["before"]["baseline_json"]), "review_sha256": scope.fingerprint,
            "adopt_audit_id": adoption_audit_id, "adopt_sha256": adoption_sha256, "target_role": role.value,
        })

    def _reassignment_before(self, raw):
        """Decode only the complete bounded canonical SQL before-image schema."""
        try:
            if type(raw) is not str or len(raw.encode()) > MAX_TEXT:
                _fail()
            value = json.loads(raw)
            if canonical(value) != raw or set(value) != {c.name for c in History.__table__.columns}:
                _fail()
            for name in ("created_at", "updated_at"):
                value[name] = datetime.fromisoformat(value[name])
            for name, cls in (("ownership", CasdoorMembershipOwnership), ("source", CasdoorMembershipSource), ("finalization", CasdoorFinalizationState)):
                value[name] = cls(value[name])
            if self._archived_encode(value) != raw:
                _fail()
            return value
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail()

    def _binding_link_facts(self, *, account_id, identity_id, namespace_id, after, lock):
        rows = self._archived_rows(Audit, sa.and_(Audit.identity_id == str(identity_id), Audit.action == "identity_link"), lock=lock)
        if not rows:
            _fail()
        values = [self._unlink_action_fact(r, action="identity_link", account_id=account_id,
                  identity_id=identity_id, namespace_id=namespace_id) for r in rows]
        if any(row["created_at"] <= after for row in values):
            _fail()
        return values

    def _retired_responsibility(self, *, account_id, namespace_id, selected_identity, lock=False, force=False):
        """Bounded retired-binding/reassignment lineage; no admission or intent exemption."""
        identities = self._archived_rows(Identity, Identity.account_id == str(account_id), lock=lock)
        by_identity = {r["id"]: r for r in identities}
        if selected_identity is not None:
            actual = by_identity.get(selected_identity["id"])
            if actual is None or not set(selected_identity) <= set(actual) or self._archived_encode(
                {k: actual[k] for k in selected_identity}
            ) != self._archived_encode(dict(selected_identity)):
                _fail()
            selected_identity = actual
        histories = self._archived_rows(History, History.account_id == str(account_id), lock=lock)
        missing = {r["id"] for r in histories if r["identity_id"] not in by_identity}
        replaced = {r["id"] for r in histories if r["identity_id"] in by_identity
                    and r["created_at"] < by_identity[r["identity_id"]]["created_at"]}
        if not missing and not replaced and not force:
            return {}, {}, ()
        # Membership IDs live inside the immutable structured fact. Read this
        # bounded private action set before selecting its exact responsibility
        # component, so foreign outer IDs cannot hide an associated forged edge.
        audits = self._archived_rows(Audit, Audit.action == "local_membership_reassignment_v1", lock=lock)
        history_map = {r["id"]: r for r in histories}
        records, facts = {}, []
        keys = {"schema_version", "membership_id", "before_history_json", "before_sha256", "release_sha256",
                "unlink_sha256", "links_json", "links_sha256", "new_identity_id", "initial_generation",
                "ownership_epoch", "join_id", "workspace_id", "baseline_sha256", "review_sha256", "adopt_audit_id",
                "adopt_sha256", "target_role", "new_identity_created_at"}
        for audit in audits:
            value = self._archived_summary(audit, keys)
            for field in ("before_sha256", "release_sha256", "unlink_sha256", "links_sha256", "adopt_sha256"):
                self._archived_hex(value[field])
            for field in ("new_identity_id", "adopt_audit_id"):
                self._archived_uuid(value[field])
            if type(value["initial_generation"]) is not int or value["initial_generation"] != 1:
                _fail()
            before = self._reassignment_before(value["before_history_json"])
            row = history_map.get(value["membership_id"])
            related_ids = set(by_identity) | {r["identity_id"] for r in histories}
            if (row is None and audit["account_id"] != str(account_id) and before["account_id"] != str(account_id)
                and not {audit["identity_id"], before["identity_id"], value["new_identity_id"]} & related_ids):
                continue
            try:
                created = datetime.fromisoformat(value["new_identity_created_at"])
                if created.tzinfo is not None or created.isoformat() != value["new_identity_created_at"]:
                    _fail()
            except (ValueError, TypeError):
                _fail()
            if (
                row is None or before["id"] != row["id"] or before["identity_id"] == value["new_identity_id"]
                or (before["namespace_id"], before["account_id"], before["workspace_id"], before["baseline_json"], before["created_at"])
                != (row["namespace_id"], row["account_id"], row["workspace_id"], row["baseline_json"], row["created_at"])
                or (audit["namespace_id"], audit["account_id"], audit["identity_id"])
                != (before["namespace_id"], str(account_id), value["new_identity_id"])
                or (value["ownership_epoch"], value["join_id"], value["workspace_id"])
                != (before["ownership_epoch"] + 1, before["join_id"], before["workspace_id"])
                or value["baseline_sha256"] != digest(before["baseline_json"])
            ):
                _fail()
            proof = self._released_unlink_proof(before, account_id=account_id, lock=lock)
            links = self._binding_link_facts(account_id=account_id, identity_id=value["new_identity_id"],
                namespace_id=before["namespace_id"], after=proof["unlink"]["created_at"], lock=lock)
            initial_links = [r for r in links if r["created_at"] < audit["created_at"]]
            encoded_links = self._archived_encode(initial_links)
            if (value["before_sha256"], value["release_sha256"], value["unlink_sha256"], value["links_json"], value["links_sha256"]) != (
                digest(value["before_history_json"]), digest(self._archived_encode(proof["release"])),
                digest(self._archived_encode(proof["unlink"])), encoded_links, digest(encoded_links)
            ):
                _fail()
            adoption = self._archived_rows(Audit, Audit.id == value["adopt_audit_id"], lock=lock)
            if len(adoption) != 1:
                _fail()
            adoption = adoption[0]
            adopted = self._archived_summary(adoption, {"schema_version", "membership_id", "ownership_epoch", "workspace_id",
                "join_id", "review_sha256", "baseline_sha256", "target_role"})
            if (
                (adoption["namespace_id"], adoption["identity_id"], adoption["account_id"], adoption["actor_account_id"],
                 adoption["revision_id"], adoption["correlation_id"], adoption["action"])
                != (audit["namespace_id"], audit["identity_id"], audit["account_id"], audit["actor_account_id"],
                    audit["revision_id"], audit["correlation_id"], "local_membership_adopt_v1")
                or any(adopted[k] != value[k] for k in ("membership_id", "ownership_epoch", "workspace_id", "join_id", "review_sha256", "baseline_sha256"))
                or not initial_links or any(r["created_at"] >= adoption["created_at"] for r in initial_links)
                or adoption["created_at"] > audit["created_at"]
                or adopted["target_role"] != value["target_role"]
                or value["adopt_sha256"] != digest(self._archived_encode(dict(adoption)))
                or not proof["unlink"]["created_at"] < created <= min(r["created_at"] for r in initial_links)
            ):
                _fail()
            revision_fact = self._unlink_revision_facts(before["namespace_id"],
                {audit["revision_id"]} | {r["revision_id"] for r in links}, lock=lock)
            records.setdefault(row["id"], []).append({"audit": dict(audit), "value": value, "before": before})
            facts.extend((self._archived_encode(dict(audit)), self._archived_encode(dict(adoption)),
                          self._archived_encode(links), proof["revision_fact"], revision_fact))
        selected_records = {}
        for membership_id, chain in records.items():
            row = history_map[membership_id]
            by_old, by_new = {}, {}
            for record in chain:
                old_id, new_id = record["before"]["identity_id"], record["value"]["new_identity_id"]
                if old_id in by_old or new_id in by_new:
                    _fail()
                by_old[old_id], by_new[new_id] = record, record
            roots = set(by_old) - set(by_new)
            if len(roots) != 1:
                _fail()
            cursor, seen, previous = next(iter(roots)), set(), None
            while cursor in by_old:
                if cursor in seen:
                    _fail()
                seen.add(cursor)
                record = by_old[cursor]
                if previous is not None and record["before"]["updated_at"] < previous["audit"]["created_at"]:
                    _fail()
                previous, cursor = record, record["value"]["new_identity_id"]
            if len(seen) != len(chain) or cursor != row["identity_id"] or row["ownership_epoch"] < previous["value"]["ownership_epoch"]:
                _fail()
            if cursor in by_identity and by_identity[cursor]["created_at"].isoformat() != previous["value"]["new_identity_created_at"]:
                _fail()
            selected_records[membership_id] = previous
        if replaced - set(records):
            _fail()
        orphans = {}
        for membership_id in missing:
            row = history_map[membership_id]
            proof = self._released_unlink_proof(row, account_id=account_id, lock=lock)
            orphans[membership_id] = proof
            facts.extend((self._archived_encode(dict(row)), self._archived_encode(proof["release"]),
                          self._archived_encode(proof["unlink"]), proof["revision_fact"]))
            if selected_identity is not None and row["namespace_id"] == str(namespace_id):
                links = self._binding_link_facts(account_id=account_id, identity_id=selected_identity["id"],
                    namespace_id=namespace_id, after=proof["unlink"]["created_at"], lock=lock)
                if not proof["unlink"]["created_at"] < selected_identity["created_at"] <= min(r["created_at"] for r in links):
                    _fail()
                proof["links"] = links
                proof["new_identity_created_at"] = selected_identity["created_at"]
                link_revisions = self._unlink_revision_facts(namespace_id,
                    {r["revision_id"] for r in links}, lock=lock)
                facts.extend((self._archived_encode(links), self._archived_encode(dict(selected_identity)), link_revisions))
        return orphans, selected_records, tuple(sorted(facts))

    def _reassignment_current_fact(self, facts, row):
        selected = []
        for encoded in facts:
            value = json.loads(encoded)
            if not isinstance(value, dict) or value.get("action") != "local_membership_reassignment_v1":
                continue
            summary = json.loads(value["summary_json"])
            if (summary["membership_id"], summary["new_identity_id"]) == (row["id"], row["identity_id"]):
                if (value["namespace_id"], value["account_id"], value["identity_id"]) != (
                    row["namespace_id"], row["account_id"], row["identity_id"]
                ):
                    _fail()
                selected.append({"value": summary})
        if len(selected) > 1:
            _fail()
        return selected[0] if selected else None

    def _adopted_current_refs(self, *, account_id, namespace_id, identity_id, workspace_id, lock=False):
        """Select exact current ADOPT only after the complete historical proof."""
        archive = self._archived_release_facts(
            account_id=account_id, namespace_id=namespace_id, identity_id=identity_id, lock=lock
        )
        if not archive[0]:
            _fail()
        rows = self._archived_rows(
            History, sa.and_(History.account_id == str(account_id), History.workspace_id == str(workspace_id)), lock=lock
        )
        current = [r for r in rows if r["id"] not in archive[3]]
        if len(current) != 1:
            _fail()
        row = current[0]
        if (row["namespace_id"], row["identity_id"], row["source"]) != (
            str(namespace_id),
            str(identity_id),
            CasdoorMembershipSource.ADOPT,
        ):
            _fail()
        for field in ("id", "namespace_id", "identity_id", "account_id", "workspace_id", "revision_id"):
            self._archived_uuid(row[field])
        self._archived_counter(row["ownership_epoch"], minimum=1)
        self._archived_counter(row["desired_generation"])
        try:
            baseline = parse_role_baseline_json(row["baseline_json"])
            if baseline.backend is not MembershipBackend.LOCAL:
                _fail()
            audits = self._archived_rows(
                Audit,
                sa.and_(
                    Audit.namespace_id == str(namespace_id),
                    Audit.account_id == str(account_id),
                    Audit.identity_id == str(identity_id),
                    Audit.action == "local_membership_adopt_v1",
                ),
                lock=lock,
            )
            # The immediately preceding SQL archive rebuild already validated
            # every private edge and full original producer fact. Select only
            # that proven current binding's unique initial edge; no cache or DTO.
            lineage = self._reassignment_current_fact(archive[0], row)
            initial = []
            for audit in audits:
                value = self._archived_summary(
                    audit,
                    {
                        "schema_version",
                        "membership_id",
                        "ownership_epoch",
                        "workspace_id",
                        "join_id",
                        "review_sha256",
                        "baseline_sha256",
                        "target_role",
                    },
                )
                expected_epoch = lineage["value"]["ownership_epoch"] if lineage is not None else 1
                if value["membership_id"] == row["id"] and value["ownership_epoch"] == expected_epoch:
                    if lineage is not None and (audit["id"] != lineage["value"]["adopt_audit_id"]
                        or audit["identity_id"] != lineage["value"]["new_identity_id"]):
                        _fail()
                    if value["workspace_id"] != str(workspace_id) or value["baseline_sha256"] != digest(
                        row["baseline_json"]
                    ):
                        _fail()
                    initial.append(audit)
            if len(initial) != 1:
                _fail()
            audit = initial[0]
            columns = tuple(
                getattr(Revision, name)
                for name in (
                    "id",
                    "integration_id",
                    "namespace_id",
                    "expected_issuer",
                    "organization",
                    "application",
                    "client_id",
                    "config_digest",
                )
            )
            revisions = self._archived_rows(Revision, Revision.id == audit["revision_id"], columns=columns)
            namespaces = self._archived_rows(Namespace, Namespace.id == str(namespace_id))
            if len(revisions) != 1 or len(namespaces) != 1:
                _fail()
            revision, namespace = revisions[0], namespaces[0]
            self._archived_hex(revision["config_digest"])
            self._archived_uuid(revision["integration_id"])
            if (
                revision["integration_id"] != namespace["integration_id"]
                or revision["namespace_id"] != str(namespace_id)
                or any(revision[f] != namespace[f] for f in ("expected_issuer", "organization", "application", "client_id"))
            ):
                _fail()
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail()
        return row["id"], archive[0] + (
            canonical({"membership_id": row["id"], "baseline_json": row["baseline_json"]}),
            self._archived_encode(dict(initial[0])),
        )

    def _archived_history(self, account_id, *, lock):
        fields = (
            "id",
            "namespace_id",
            "identity_id",
            "account_id",
            "workspace_id",
            "join_id",
            "ownership",
            "ownership_epoch",
            "source",
            "desired_generation",
            "revision_id",
            "last_applied_roles_json",
            "last_applied_fingerprint",
            "desired_roles_json",
            "baseline_json",
            "finalization",
            "tombstone",
        )
        result = []
        for row in self._archived_rows(History, History.account_id == str(account_id), lock=lock):
            try:
                baseline = parse_role_baseline_json(row["baseline_json"])
                applied = parse_role_baseline_json(row["last_applied_roles_json"])
                if (
                    baseline.backend is not MembershipBackend.LOCAL
                    or applied.backend is not MembershipBackend.LOCAL
                    or row["tombstone"]
                    or row["finalization"] is not CasdoorFinalizationState.FINALIZED
                ):
                    _fail()
                self._archived_counter(row["ownership_epoch"])
            except (ValueError, TypeError, AttributeError):
                _fail()
            result.append({key: row[key] for key in fields})
        return tuple(result)
