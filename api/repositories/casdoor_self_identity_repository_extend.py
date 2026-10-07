"""Bounded self-only scalar observations; never a mutation or remote authority owner.

Every probe is rooted in the authenticated account. Large text is measured before
materialization and guarded again in SQL. Readbacks detect observed association
changes without locks; they do not promise a globally atomic multi-page snapshot.
"""

import json
import re
from uuid import UUID

import sqlalchemy as sa
from extensions.storage.storage_type import StorageType
from models.account import Account, Tenant, TenantAccountJoin
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
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
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.enums import CreatorUserRole
from models.model import UploadFile
from sqlalchemy.orm import Session

from repositories.casdoor_audit_repository_extend import (
    _AVATAR_PRE_STORAGE_SUMMARY_BYTES,
    _AVATAR_RETRY_SUMMARY_BYTES,
    _pre_storage_summary_v2,
)
from repositories.casdoor_avatar_repository_extend import (
    _RETRY_HISTORY_FIELDS,
    _RETRY_IDENTITY_FIELDS,
    _RETRY_JOIN_FIELDS,
    _RETRY_NAMESPACE_FIELDS,
    _RETRY_REVISION_FIELDS,
    MAX_DESIRED_BYTES,
    _pre_storage_intent_digest,
    _retry_claim_values,
    _retry_lineage_values,
    _retry_scope_values,
    _retry_shape,
    _worker_state,
    _worker_time,
)
from repositories.casdoor_configuration_repository_extend import _POLICY_FIELDS
from repositories.casdoor_profile_repository_extend import _parse_time


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate observation key")
        result[key] = value
    return result


def _avatar_policy(raw):
    """The closed persisted policy subset, without loading configuration secrets."""
    value = json.loads(raw, object_pairs_hook=_unique_pairs)
    if (
        type(value) is not dict
        or set(value) != set(_POLICY_FIELDS)
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != "openid email profile"
        or value["default_normal_fallback"] is not True
        or value["name_sync"] not in ("off", "fill_empty", "managed")
        or value["avatar_mode"] not in ("fill_empty", "managed")
        or any(type(value[key]) is not bool for key in ("avatar_sync", "rp_logout", "self_unlink"))
    ):
        raise ValueError("invalid observation policy")
    return value


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

    def avatar_account(self):
        """Private local value for correspondence checks, never returned as a file ID."""
        rows = self.read(
            sa.select(
                Account.id, self.bounded(Account.avatar, 255), self._length(Account.avatar).label("avatar_length")
            )
            .where(Account.id == self.account_id)
            .limit(1)
        )
        if len(rows) != 1:
            raise CasdoorSelfReadConflict()
        if rows[0]["avatar_length"] is not None and rows[0]["avatar_length"] > 255:
            raise CasdoorSelfReadConflict()
        return dict(rows[0])

    def _avatar_revision(self, revision_id, namespace):
        rows = self.read(
            sa.select(
                Revision.id,
                Revision.namespace_id,
                Revision.default_workspace_id,
                Revision.schema_version,
                self.bounded(Revision.policy_json, 4096),
            )
            .where(
                Revision.id == revision_id,
                Revision.namespace_id == namespace["id"],
                Revision.integration_id == namespace["integration_id"],
                sa.exists(
                    sa.select(Identity.id).where(
                        Identity.account_id == self.account_id,
                        Identity.namespace_id == Revision.namespace_id,
                    )
                ),
                sa.exists(
                    sa.select(Namespace.id).where(
                        Namespace.id == Revision.namespace_id,
                        Namespace.expected_issuer == Revision.expected_issuer,
                        Namespace.organization == Revision.organization,
                        Namespace.application == Revision.application,
                        Namespace.client_id == Revision.client_id,
                    )
                ),
            )
            .limit(1)
        )
        if len(rows) != 1 or type(rows[0]["schema_version"]) is not int or rows[0]["schema_version"] != 1:
            raise ValueError("invalid avatar revision")
        result = dict(rows[0])
        if str(UUID(result["default_workspace_id"])) != result["default_workspace_id"]:
            raise ValueError("invalid avatar workspace")
        result["policy"] = _avatar_policy(result.pop("policy_json"))
        return result

    def _avatar_intent(self, key, scope):
        columns = []
        for column in Intent.__table__.columns:
            if column.key == "desired_json":
                column = self.bounded(column, MAX_DESIRED_BYTES)
            elif column.key in ("kind", "operation_state", "termination_state"):
                column = sa.cast(column, sa.String).label(column.key)
            columns.append(column)
        rows = self.read(sa.select(*columns).where(Intent.id == key, *scope).limit(1))
        if len(rows) != 1:
            raise ValueError("avatar observation disappeared")
        return dict(rows[0])

    def _avatar_audit(self, row, data, action):
        """Enumerate scalar IDs first, then SQL-bound one closed summary."""
        predicate = (
            Audit.account_id == self.account_id,
            Audit.correlation_id == data["correlation_id"],
            Audit.action == action,
        )
        candidates = self.read(sa.select(Audit.id).where(*predicate).order_by(Audit.id).limit(2))
        if len(candidates) != 1:
            raise ValueError("ambiguous avatar audit")
        rows = self.read(
            sa.select(
                Audit.namespace_id,
                Audit.revision_id,
                Audit.identity_id,
                Audit.account_id,
                Audit.actor_account_id,
                Audit.result_code,
                Audit.created_at,
                self.bounded(Audit.summary_json, _AVATAR_PRE_STORAGE_SUMMARY_BYTES),
            )
            .where(Audit.id == candidates[0]["id"], *predicate)
            .limit(1)
        )
        if len(rows) != 1:
            raise ValueError("missing avatar audit")
        audit = rows[0]
        if (
            any(audit[key] != row[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id"))
            or audit["actor_account_id"] is not None
        ):
            raise ValueError("foreign avatar audit")
        _worker_time(audit["created_at"])
        refs = {key: row[key] for key in ("namespace_id", "revision_id", "identity_id", "account_id")}
        refs.update(intent_id=row["id"], attempt_id=row["attempt_id"], file_id=data["reservations"][-1]["file_id"])
        if action == "avatar_pre_storage":
            summary = _pre_storage_summary_v2(audit["summary_json"])
            expected = dict(
                schema_version=2,
                references=refs,
                count=row["attempt_count"],
                generation=row["generation"],
                fence_epoch=row["fence_epoch"],
                reason=row["error_code"],
                proof_ref=row["proof_ref"],
                terminal_intent_sha256=_pre_storage_intent_digest(row),
            )
            result_code = "failed"
        else:
            summary = json.loads(audit["summary_json"], object_pairs_hook=_unique_pairs)
            expected = dict(
                schema_version=1,
                references=refs,
                count=row["attempt_count"],
                generation=row["generation"],
                fence_epoch=row["fence_epoch"],
                reason="attached",
            )
            # Exact canonical bytes distinguish bool from int as well as extra keys.
            if json.dumps(summary, sort_keys=True, separators=(",", ":")) != audit["summary_json"]:
                raise ValueError("noncanonical attachment audit")
            result_code = "applied"
        if audit["result_code"] != result_code or json.dumps(
            summary, sort_keys=True, separators=(",", ":")
        ) != json.dumps(expected, sort_keys=True, separators=(",", ":")):
            raise ValueError("invalid avatar audit")

    def _avatar_attachment(self, row, data, revision):
        """Strict local DB attachment observation, never physical storage proof."""
        if _retry_shape(row, data):
            self._avatar_retry_lineage(row, data)
        last = data["reservations"][-1]
        if (
            row["termination_state"] != "confirmed"
            or row["termination_proof_kind"] != "avatar_attachment_db"
            or row["resource_id"] != data["result_file_id"]
            or row["proof_ref"] != last["file_id"]
            or data["result_file_id"] != last["file_id"]
            or row["readback_at"] != row["terminated_at"]
            or row["updated_at"] != row["terminated_at"]
            or data["cleanup_state"] != "none"
            or (
                any(item["cleanup_state"] != "none" for item in data["reservations"])
                and not (
                    _retry_shape(row, data)
                    and row["attempt_count"] == 2
                    and data["reservations"][-1]["cleanup_state"] == "none"
                )
            )
            or any(
                row[key] is not None
                for key in ("sent_at", "acknowledged_at", "retry_at", "error_code")
            )
        ):
            raise ValueError("invalid attachment shape")
        terminated = _worker_time(row["terminated_at"])
        if (
            not _parse_time(data["url_created_at"])
            <= terminated
            < _parse_time(data["url_expires_at"])
        ):
            raise ValueError("invalid attachment time")
        if terminated >= _worker_time(row["lease_expires_at"]):
            raise ValueError("invalid attachment lease")
        rows = self.read(
            sa.select(
                UploadFile.id,
                UploadFile.size,
                self.bounded(UploadFile.hash, 64),
                UploadFile.created_at,
                UploadFile.used_at,
            )
            .where(
                UploadFile.id == last["file_id"],
                UploadFile.tenant_id == revision["default_workspace_id"],
                UploadFile.created_by == self.account_id,
                UploadFile.used_by == self.account_id,
                UploadFile.created_by_role == CreatorUserRole.ACCOUNT,
                UploadFile.key == last["storage_key"],
                UploadFile.name == "avatar.png",
                UploadFile.extension == "png",
                UploadFile.mime_type == "image/png",
                UploadFile.source_url == "",
                sa.cast(UploadFile.storage_type, sa.String).in_(tuple(StorageType)),
                sa.cast(UploadFile.used, sa.Integer) == 1,
            )
            .limit(1)
        )
        if (
            len(rows) != 1
            or type(rows[0]["size"]) is not int
            or not 1 <= rows[0]["size"] <= 2097152
        ):
            raise ValueError("missing owned attachment file")
        if type(rows[0]["hash"]) is not str or not re.fullmatch(
            r"[0-9a-f]{64}", rows[0]["hash"]
        ):
            raise ValueError("invalid attachment hash")
        _worker_time(rows[0]["created_at"])
        _worker_time(rows[0]["used_at"])
        self._avatar_audit(row, data, "avatar_attach")

    def avatar_observation(self, identity, account):
        """Finite private facts for the self service; malformed facts stay unknown.

        All selected facts join this account; every statement participates in the
        existing final recheck. This reader never enters a worker root or issues a
        capability. A digest match retains evidence only, not recovery authority.
        """
        try:
            ns = identity["namespace"]
            if (
                ns is None
                or ns["id"] != identity["namespace_id"]
                or not identity["issuer_matches"]
                or not identity["organization_matches"]
                or ns["integration_present"] is None
                or ns["slot"] != 1
                or ns["enabled"] not in (0, 1)
                or ns["lifecycle"] not in ("active", "fencing", "archived")
                or type(identity["sync_generation"]) is not int
                or not 0 <= identity["sync_generation"] <= 2**63 - 1
                or type(ns["fence_epoch"]) is not int
                or not 0 <= ns["fence_epoch"] <= 2**63 - 1
            ):
                raise ValueError("invalid avatar parent")
            for key in ("id", "namespace_id"):
                if str(UUID(identity[key])) != identity[key]:
                    raise ValueError("invalid avatar identity")
            scope = (
                Intent.account_id == self.account_id,
                Intent.identity_id == identity["id"],
                Intent.namespace_id == identity["namespace_id"],
                Intent.kind == "profile_avatar",
            )
            candidates = self.read(
                sa.select(Intent.id, Intent.generation)
                .where(*scope)
                .order_by(Intent.generation.desc(), Intent.id)
                .limit(2)
            )
            active = (
                ns["enabled"] == 1
                and ns["lifecycle"] == "active"
                and ns["active_namespace_id"] == ns["id"]
            )
            if not candidates:
                if not active:
                    return {"current": False, "intent": None}
                revision = self._avatar_revision(ns["active_revision_id"], ns)
                return {
                    "current": True,
                    "intent": None,
                    "enabled": revision["policy"]["avatar_sync"],
                }
            if (
                any(type(item["generation"]) is not int for item in candidates)
                or candidates[0]["generation"] > identity["sync_generation"]
                or (
                    len(candidates) == 2
                    and candidates[0]["generation"] == candidates[1]["generation"]
                )
            ):
                raise ValueError("ambiguous avatar generation")
            row = self._avatar_intent(candidates[0]["id"], scope)
            data = _worker_state(row)
            _worker_time(row["created_at"])
            _worker_time(row["updated_at"])
            revision = self._avatar_revision(row["revision_id"], ns)
            current = (
                active
                and row["revision_id"] == ns["active_revision_id"]
                and row["generation"] == identity["sync_generation"]
                and row["fence_epoch"] == ns["fence_epoch"]
            )
            if current and data["policy"] != revision["policy"]["avatar_mode"]:
                raise ValueError("avatar mode drift")
            cleanup_complete = False
            if row["termination_proof_kind"] == "avatar_cleanup":
                from repositories.casdoor_self_avatar_cleanup_observation_extend import completed_cleanup

                cleanup_complete = completed_cleanup(self, row, data, revision, current=current)
            if (
                _retry_shape(row, data)
                and row["termination_proof_kind"] not in ("avatar_pre_storage", "avatar_cleanup")
            ):
                self._avatar_retry_scope(row)
                self._avatar_retry_lineage(row, data)
            state = row["operation_state"]
            if state == "failed":
                if not cleanup_complete:
                    if row["termination_proof_kind"] != "avatar_pre_storage":
                        raise ValueError("unproven avatar failure")
                    self._avatar_audit(row, data, "avatar_pre_storage")
            elif state == "applied":
                self._avatar_attachment(row, data, revision)
            elif state not in ("pending", "in_flight", "unknown") or (
                state == "pending"
                and row["attempt_count"] != 0
                and row["termination_proof_kind"] != "avatar_retry_pre_storage"
            ):
                raise ValueError("unsupported avatar state")
            return {
                "current": current,
                "intent": row,
                "data": data,
                "enabled": revision["policy"]["avatar_sync"],
                "cleanup_complete": cleanup_complete,
                "local_differs": account["avatar"] != data["result_file_id"]
                if state == "applied"
                else None,
            }
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            UnicodeError,
            RecursionError,
        ):
            return None

    def _avatar_retry_lineage(self, row, data):
        """Tracked bounded facts only; never enter worker locks or mint authority."""

        def audit(action, correlation, maximum):
            ids = self.read(
                sa.select(Audit.id)
                .where(Audit.action == action, Audit.correlation_id == correlation)
                .order_by(Audit.id)
                .limit(2)
            )
            if len(ids) != 1:
                raise ValueError("ambiguous retry audit")
            columns = [
                self.bounded(
                    column, maximum if column.name == "summary_json" else 64
                ).label(column.name)
                if isinstance(column.type, (sa.String, sa.Text))
                or column.name
                in (
                    "id",
                    "namespace_id",
                    "revision_id",
                    "identity_id",
                    "account_id",
                    "actor_account_id",
                    "correlation_id",
                )
                else column
                for column in Audit.__table__.columns
            ]
            rows = self.read(sa.select(*columns).where(Audit.id == ids[0]["id"]).limit(1))
            if len(rows) != 1:
                raise ValueError("missing retry audit")
            return dict(rows[0])

        retry = audit("avatar_retry", row["id"], _AVATAR_RETRY_SUMMARY_BYTES)
        original = audit(
            "avatar_pre_storage", data["correlation_id"], _AVATAR_PRE_STORAGE_SUMMARY_BYTES
        )
        _retry_lineage_values(row, data, retry, original)
        if row["attempt_count"] == 1 and self.read(
            sa.select(Audit.id)
            .where(Audit.action == "avatar_retry_claim", Audit.correlation_id == row["id"])
            .limit(1)
        ):
            raise ValueError("unexpected avatar retry claim")
        if row["attempt_count"] == 2:
            _retry_claim_values(
                row,
                data,
                retry,
                audit("avatar_retry_claim", row["id"], _AVATAR_RETRY_SUMMARY_BYTES),
            )

    def _avatar_retry_scope(self, row):
        def rows(model, fields, where):
            columns = [
                self.bounded(
                    getattr(model, key),
                    2048 if key in ("issuer", "expected_issuer") else 255,
                ).label(key)
                if isinstance(model.__table__.columns[key].type, (sa.String, sa.Text))
                or key == "id"
                or key.endswith("_id")
                else getattr(model, key)
                for key in fields
            ]
            values = self.read(
                sa.select(*columns).where(where).order_by(model.id).limit(101)
            )
            if len(values) > 100:
                raise ValueError("avatar retry scope is unavailable")
            return [dict(value) for value in values]

        account_id = row["account_id"]
        histories = rows(
            Membership, _RETRY_HISTORY_FIELDS, Membership.account_id == account_id
        )
        identities = rows(
            Identity,
            _RETRY_IDENTITY_FIELDS,
            sa.or_(
                Identity.account_id == account_id,
                Identity.id.in_([item["identity_id"] for item in histories]),
            ),
        )
        joins = rows(
            TenantAccountJoin,
            _RETRY_JOIN_FIELDS,
            TenantAccountJoin.account_id == account_id,
        )
        namespaces = rows(
            Namespace,
            _RETRY_NAMESPACE_FIELDS,
            Namespace.id.in_({item["namespace_id"] for item in (*identities, *histories)}),
        )
        revisions = rows(
            Revision,
            _RETRY_REVISION_FIELDS,
            Revision.id.in_({item["revision_id"] for item in histories}),
        )
        workspaces = rows(
            Tenant,
            ("id", "status"),
            Tenant.id.in_(
                {item["tenant_id"] for item in joins}
                | {item["workspace_id"] for item in histories}
            ),
        )
        current_revision = rows(
            Revision, ("id", "integration_id"), Revision.id == row["revision_id"]
        )
        if len(current_revision) != 1:
            raise ValueError("avatar retry scope is unavailable")
        _retry_scope_values(
            account_id,
            current_revision[0]["integration_id"],
            identities,
            joins,
            histories,
            namespaces,
            revisions,
            workspaces,
        )
        where = sa.or_(
            Intent.account_id == account_id,
            Intent.identity_id.in_([item["id"] for item in identities]),
            Intent.membership_id.in_([item["id"] for item in histories]),
        )
        intents = rows(
            Intent,
            ("id", "kind", "operation_state", "termination_state", "revision_id"),
            where,
        )
        # Load desired JSON separately through the existing large bound, never truncate a proof.
        for intent in intents:
            if intent["id"] == row["id"]:
                continue
            if (
                intent["kind"] != "profile_avatar"
                or intent["operation_state"] != "applied"
                or intent["termination_state"] != "confirmed"
            ):
                raise ValueError("avatar retry scope is unavailable")
            candidate = self.read(
                sa.select(
                    *[
                        self.bounded(
                            column,
                            MAX_DESIRED_BYTES if column.name == "desired_json" else 2048,
                        ).label(column.name)
                        if isinstance(column.type, (sa.String, sa.Text))
                        or column.name == "id"
                        or column.name.endswith("_id")
                        else column
                        for column in Intent.__table__.columns
                    ]
                )
                .where(Intent.id == intent["id"])
                .limit(1)
            )
            revision = self.read(
                sa.select(Revision.id, Revision.namespace_id, Revision.default_workspace_id)
                .where(Revision.id == intent["revision_id"])
                .limit(1)
            )
            if len(candidate) != 1 or len(revision) != 1:
                raise ValueError("avatar retry scope is unavailable")
            other = dict(candidate[0])
            if other["account_id"] != account_id or not any(
                identity["id"] == other["identity_id"]
                and identity["namespace_id"] == other["namespace_id"]
                for identity in identities
            ):
                raise ValueError("foreign related avatar")
            self._avatar_attachment(other, _worker_state(other), dict(revision[0]))

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
                sa.select(Identity.id).where(Identity.id == Membership.identity_id, Identity.account_id != self.account_id)
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
                .outerjoin(Identity, sa.and_(Identity.id == Membership.identity_id, Identity.account_id == self.account_id))
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
            if len(item["history_refs"]) > 1 or (item["namespace"] and item["namespace"]["lifecycle"] == "archived"):
                item["_archived_observation"] = self._archived_membership_observation(item)
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


    def _archived_membership_observation(self, item):
        """Private SELECT-only historical classification, never mutation authority."""
        import hashlib

        from core.casdoor.auth_transactions import AuthTransactionError
        from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository

        reader = self

        class ObservedSession:
            def get_bind(self):
                return reader.session.get_bind()

            def execute(self, statement, *args, **kwargs):
                frozen = reader.session.execute(statement, *args, **kwargs).freeze()
                reader.observations.append((statement, tuple(frozen().mappings())))
                return frozen()

        try:
            current = self.read(
                sa.select(Identity.id, Identity.namespace_id)
                .join(Namespace, Namespace.id == Identity.namespace_id)
                .join(Integration, Integration.id == Namespace.integration_id)
                .join(Revision, Revision.id == Integration.active_revision_id)
                .where(Identity.account_id == self.account_id, Revision.namespace_id == Namespace.id)
                .limit(2)
            )
            if len(current) != 1:
                return None
            identity = current[0]
            owner = CasdoorLocalLifecycleRepository(ObservedSession())
            archive = owner._archived_release_facts(
                account_id=self.account_id,
                namespace_id=identity["namespace_id"],
                identity_id=identity["id"],
            )
            if item["id"] in archive[3]:
                return item["id"], "released", hashlib.sha256("".join(archive[0]).encode()).hexdigest()
            membership_id, facts = owner._adopted_current_refs(
                account_id=self.account_id,
                namespace_id=identity["namespace_id"],
                identity_id=identity["id"],
                workspace_id=item["workspace_id"],
            )
            if membership_id == item["id"]:
                return membership_id, "current", hashlib.sha256("".join(facts).encode()).hexdigest()
        except (AuthTransactionError, ValueError, TypeError, KeyError, AttributeError):
            return None
        return None
