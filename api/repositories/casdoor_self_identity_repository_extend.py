"""Bounded self-only scalar observations; never a mutation or remote authority owner.

Every probe is rooted in the authenticated account. Large text is measured before
materialization and guarded again in SQL. Readbacks detect observed association
changes without locks; they do not promise a globally atomic multi-page snapshot.
"""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from models.account import Account, Tenant, TenantAccountJoin
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as Membership,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)


class CasdoorSelfReadConflict(ValueError):
    def __init__(self):
        super().__init__("casdoor_self_read_conflict")


class CasdoorSelfIdentityRepository:
    def __init__(self, session: Session, account_id: str):
        self.session = session
        self.account_id = account_id
        self.observations = []

    def read(self, statement):
        rows = tuple(self.session.execute(statement).mappings())
        self.observations.append((statement, rows))
        return rows

    def recheck(self):
        for statement, rows in self.observations:
            if tuple(self.session.execute(statement).mappings()) != rows:
                raise CasdoorSelfReadConflict()

    def _length(self, column):
        if self.session.get_bind().dialect.name == "sqlite":
            return sa.func.length(sa.cast(column, sa.LargeBinary))
        return sa.func.octet_length(column)

    def bounded(self, column, maximum):
        # SQL CASE prevents allocating an oversized stored value in Python even
        # if it changed after the separate length probe.
        return sa.case((self._length(column) <= maximum, column), else_=None).label(column.key)

    def texts(self, model, predicate, bounds):
        self.read(sa.select(*(self._length(getattr(model, k)).label(k) for k in bounds)).where(*predicate).limit(1))
        rows = self.read(
            sa.select(*(self.bounded(getattr(model, k), n) for k, n in bounds.items())).where(*predicate).limit(1)
        )
        if not rows:
            raise CasdoorSelfReadConflict()
        return dict(rows[0])

    def page(self, model, after, limit, columns):
        query = sa.select(*columns).where(model.account_id == self.account_id)
        if after is not None:
            query = query.where(model.id > str(after))
        rows = self.read(query.order_by(model.id).limit(limit + 1))
        for row in rows:
            try:
                if str(UUID(row["id"])) != row["id"]:
                    raise CasdoorSelfReadConflict()
            except (ValueError, TypeError, AttributeError):
                raise CasdoorSelfReadConflict() from None
        more = len(rows) > limit
        return rows[:limit], more, rows[limit - 1]["id"] if more else None

    def account(self):
        return self.texts(Account, (Account.id == self.account_id,), {"name": 255, "email": 255})

    def linked(self):
        return bool(self.read(sa.select(Identity.id).where(Identity.account_id == self.account_id).limit(1)))

    def namespace(self, namespace_id):
        owned = sa.or_(
            sa.exists(
                sa.select(Identity.id).where(
                    Identity.account_id == self.account_id, Identity.namespace_id == Namespace.id
                )
            ),
            sa.exists(
                sa.select(Membership.id).where(
                    Membership.account_id == self.account_id, Membership.namespace_id == Namespace.id
                )
            ),
        )
        rows = self.read(
            sa.select(
                Namespace.id,
                Namespace.integration_id,
                Namespace.fence_epoch,
                sa.cast(Namespace.lifecycle, sa.String).label("lifecycle"),
                Integration.id.label("integration_present"),
                sa.cast(Integration.enabled, sa.Integer).label("enabled"),
                Integration.slot,
                Integration.active_revision_id,
                Revision.id.label("active_revision_present"),
                Revision.namespace_id.label("active_namespace_id"),
                Revision.integration_id.label("active_integration_id"),
                (Revision.expected_issuer == Namespace.expected_issuer).label("active_issuer_matches"),
                (Revision.organization == Namespace.organization).label("active_organization_matches"),
                (Revision.application == Namespace.application).label("active_application_matches"),
                (Revision.client_id == Namespace.client_id).label("active_client_matches"),
            )
            .outerjoin(Integration, Integration.id == Namespace.integration_id)
            .outerjoin(Revision, Revision.id == Integration.active_revision_id)
            .where(Namespace.id == namespace_id, owned)
            .limit(1)
        )
        return dict(rows[0]) if rows else None

    def identities(self, after, limit):
        rows, more, cursor = self.page(
            Identity, after, limit, (Identity.id, Identity.namespace_id, Identity.sync_generation)
        )
        result = []
        for row in rows:
            own = (
                Identity.id == row["id"],
                Identity.account_id == self.account_id,
                Identity.namespace_id == row["namespace_id"],
            )
            item = dict(row)
            item.update(
                self.texts(
                    Identity,
                    own,
                    {"organization": 255, "remote_email": 255, "last_applied_json": 4096, "profile_sync_json": 4096},
                )
            )
            # Cast Boolean to integer so corrupt persisted integers are not
            # coerced to True by SQLAlchemy's Boolean result processor.
            detail = self.read(
                sa.select(
                    sa.cast(Identity.email_verified, sa.Integer).label("email_verified"),
                    (Identity.issuer == Namespace.expected_issuer).label("issuer_matches"),
                    (Identity.organization == Namespace.organization).label("organization_matches"),
                )
                .outerjoin(Namespace, Namespace.id == Identity.namespace_id)
                .where(*own)
                .limit(1)
            )
            if not detail:
                raise CasdoorSelfReadConflict()
            item.update(detail[0])
            item["namespace"] = self.namespace(row["namespace_id"])
            result.append(item)
        return result, more, cursor

    def joins(self, workspace_id):
        return self.read(
            sa.select(
                TenantAccountJoin.id,
                sa.cast(TenantAccountJoin.role, sa.String).label("role"),
                Tenant.id.label("workspace_present"),
            )
            .outerjoin(Tenant, Tenant.id == TenantAccountJoin.tenant_id)
            .where(TenantAccountJoin.account_id == self.account_id, TenantAccountJoin.tenant_id == workspace_id)
            .order_by(TenantAccountJoin.id)
            .limit(2)
        )

    def history_refs(self, workspace_id, namespace_id=None):
        query = sa.select(Membership.id).where(
            Membership.account_id == self.account_id, Membership.workspace_id == workspace_id
        )
        if namespace_id is not None:
            query = query.where(Membership.namespace_id == namespace_id)
        return self.read(query.order_by(Membership.id).limit(2))

    def memberships(self, after, limit):
        rows, more, cursor = self.page(
            Membership,
            after,
            limit,
            (
                Membership.id,
                Membership.namespace_id,
                Membership.identity_id,
                Membership.workspace_id,
                Membership.join_id,
                Membership.revision_id,
                Membership.ownership_epoch,
                Membership.desired_generation,
                sa.cast(Membership.ownership, sa.String).label("ownership"),
                sa.cast(Membership.source, sa.String).label("source"),
                sa.cast(Membership.finalization, sa.String).label("finalization"),
                sa.cast(Membership.tombstone, sa.Integer).label("tombstone"),
            ),
        )
        result = []
        for row in rows:
            own = (Membership.id == row["id"], Membership.account_id == self.account_id)
            item = dict(row)
            item.update(
                self.texts(
                    Membership,
                    own,
                    {
                        "last_applied_roles_json": 65535,
                        "desired_roles_json": 65535,
                        "baseline_json": 65535,
                        "last_applied_fingerprint": 64,
                    },
                )
            )
            # All associations are predicates of this own historical row. Only
            # existence of a foreign identity/join is observed, never its fields.
            foreign_identity = sa.exists(
                sa.select(Identity.id).where(
                    Identity.id == Membership.identity_id, Identity.account_id != self.account_id
                )
            ).correlate(Membership)
            removed_present = sa.exists(
                sa.select(TenantAccountJoin.id).where(TenantAccountJoin.id == Membership.join_id)
            ).correlate(Membership)
            chain = self.read(
                sa.select(
                    Revision.namespace_id.label("revision_namespace"),
                    Revision.integration_id.label("revision_integration"),
                    Identity.id.label("own_identity"),
                    Identity.namespace_id.label("identity_namespace"),
                    Identity.sync_generation.label("identity_generation"),
                    (Identity.issuer == Namespace.expected_issuer).label("identity_issuer_matches"),
                    (Identity.organization == Namespace.organization).label("identity_organization_matches"),
                    (Revision.expected_issuer == Namespace.expected_issuer).label("revision_issuer_matches"),
                    (Revision.organization == Namespace.organization).label("revision_organization_matches"),
                    (Revision.application == Namespace.application).label("revision_application_matches"),
                    (Revision.client_id == Namespace.client_id).label("revision_client_matches"),
                    Tenant.id.label("workspace_present"),
                    foreign_identity.label("foreign_identity"),
                    removed_present.label("removed_present"),
                )
                .select_from(Membership)
                .outerjoin(Namespace, Namespace.id == Membership.namespace_id)
                .outerjoin(Revision, Revision.id == Membership.revision_id)
                .outerjoin(
                    Identity, sa.and_(Identity.id == Membership.identity_id, Identity.account_id == self.account_id)
                )
                .outerjoin(Tenant, Tenant.id == Membership.workspace_id)
                .where(*own)
                .limit(1)
            )
            if not chain:
                raise CasdoorSelfReadConflict()
            item.update(chain[0])
            item["namespace"] = self.namespace(row["namespace_id"])
            item["joins"] = self.joins(row["workspace_id"])
            item["history_refs"] = self.history_refs(row["workspace_id"])
            item["scope_refs"] = self.history_refs(row["workspace_id"], row["namespace_id"])
            result.append(item)
        return result, more, cursor

    def current_memberships(self, after, limit):
        rows, more, cursor = self.page(
            TenantAccountJoin, after, limit, (TenantAccountJoin.id, TenantAccountJoin.tenant_id)
        )
        result = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "workspace_id": row["tenant_id"],
                    "joins": self.joins(row["tenant_id"]),
                    "history_refs": self.history_refs(row["tenant_id"]),
                }
            )
        return result, more, cursor
