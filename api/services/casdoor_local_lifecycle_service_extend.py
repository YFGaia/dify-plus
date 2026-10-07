"""Instance-manager LOCAL review/release/adopt, never ordinary admission.

The review identifier is a one-use server handoff, not authentication. Each caller
revalidates original access/account/request-refresh and exact SQL scope. Only the
manager-only target directory producer below accepts a DB-owned subject; it never
constructs a VerifiedIDToken or produces protocol validation/session eligibility.
"""

import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.claims import StructuredUserRef, VerifiedOnlineUser
from core.casdoor.gateway import CasdoorDirectoryGateway
from core.casdoor.leases import CasdoorLeases, CasdoorLeaseScope, WorkspaceMemberScope
from core.casdoor.request_safety import RequestAction
from core.casdoor.role_graph import compute_effective_roles
from enums import DeploymentEdition
from models.account import TenantAccountRole
from repositories.casdoor_identity_lifecycle_repository_extend import CasdoorIdentityLifecycleRepository
from repositories.casdoor_local_lifecycle_repository_extend import CasdoorLocalLifecycleRepository, canonical, digest

from services.casdoor_identity_action_service_extend import CasdoorIdentityActionService

_REVIEW_PREFIX = "casdoor:local-maintenance-review:v1:"
_CONSUME_REVIEW = "local v=redis.call('GET',KEYS[1]); if v then redis.call('DEL',KEYS[1]) end; return v"


@dataclass(frozen=True, repr=False)
class TargetDirectorySnapshot:
    """Actual DB-subject directory result; expressly not an authentication proof."""

    roles: object
    fingerprint: str


class CasdoorLocalLifecycleService(CasdoorIdentityActionService):
    def list_memberships(self, account, *, refresh_token, server_ip, **pagination):
        deadline = time.monotonic() + 45.0

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            with self._session_factory() as session, session.begin():
                result = CasdoorLocalLifecycleRepository(session).candidates(**pagination)
            if (
                self._source(account, refresh_token, client, management=True) != source
                or time.monotonic() >= owned_deadline
            ):
                raise AuthTransactionError("source_session_invalid")
            return result

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def _local_mode(self):
        if (
            self._settings.RBAC_ENABLED is not False
            or self._configuration_service._rbac_enabled is not False
            or self._settings.DEPLOYMENT_EDITION != DeploymentEdition.COMMUNITY
        ):
            raise AuthTransactionError("local_mode_required")

    def _read_scope(self, identity_id, workspace_id, source_account_id):
        with self._session_factory() as session, session.begin():
            return CasdoorLocalLifecycleRepository(session).inspect(
                identity_id, workspace_id, source_account_id=source_account_id
            )

    def _maintenance_guard(self, scope, source, account, refresh_token, client, deadline):
        self._local_mode()
        if time.monotonic() >= deadline or self._source(account, refresh_token, client, management=True) != source:
            raise AuthTransactionError("source_session_invalid")
        current = self._read_scope(scope.identity_id, scope.workspace_id, source.account_id)
        if current.fingerprint != scope.fingerprint or time.monotonic() >= deadline:
            raise AuthTransactionError("context_changed")

    @staticmethod
    def _lease_scope(scope, source):
        workspaces = (
            {scope.workspace_id}
            | {UUID(row["workspace_id"]) for row in scope.histories}
            | {UUID(value) for value in scope._historical_workspace_ids}
        )
        current = CasdoorLeaseScope(
            scope.namespace_id,
            scope.subject,
            account_ids=tuple(sorted({scope.account_id, source.account_id}, key=str)),
            members=tuple(WorkspaceMemberScope(value, scope.account_id) for value in sorted(workspaces, key=str)),
        )
        return (current,) + tuple(
            CasdoorLeaseScope(UUID(namespace_id), subject, account_ids=current.account_ids, members=current.members)
            for namespace_id, subject in scope._archived_subjects
        )

    def _target_directory(self, scope, snapshot, deadline, guard):
        """Real stable id is mandatory; a username-shaped snapshot is inadequate."""
        if snapshot.binding.namespace_id != scope.namespace_id:
            raise AuthTransactionError("context_changed")
        guard()
        operation = self._operation(snapshot, deadline)
        directory = CasdoorDirectoryGateway(
            operation, verified_subject=scope.subject, credential_strategy=snapshot.policy.credential_strategy
        )
        raw_organization = directory.get_organization_visibility()
        guard()
        raw_user = directory.get_verified_user()
        if (
            raw_user.get("id") != scope.subject
            or raw_user.get("owner") != snapshot.configuration.organization
            or raw_user.get("isForbidden") is not False
            or raw_user.get("isDeleted") is not False
        ):
            raise AuthTransactionError("target_online_unknown")
        guard()
        raw_roles = directory.get_complete_roles()
        guard()
        online = VerifiedOnlineUser(
            scope.subject, StructuredUserRef(snapshot.configuration.organization, raw_user["name"])
        )
        roles = compute_effective_roles(
            organization=snapshot.configuration.organization,
            verified_subject=scope.subject,
            online_user=online,
            raw_user=raw_user,
            raw_roles=raw_roles,
            raw_organization=raw_organization,
            contract=snapshot.policy.directory_snapshot_contract,
        )
        guard()
        # Hash graph-derived grants only; do not retain user/profile/org payloads.
        return TargetDirectorySnapshot(
            roles,
            digest(
                {
                    "subject_sha256": digest(scope.subject),
                    "roles": sorted((role.organization, role.name) for role in roles.effective_roles),
                }
            ),
        )

    def inspect_membership(self, account, *, identity_id, workspace_id, refresh_token, server_ip):
        deadline = time.monotonic() + 45.0

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            scope = self._read_scope(identity_id, workspace_id, source.account_id)
            self._maintenance_guard(scope, source, account, refresh_token, client, owned_deadline)
            histories = [row for row in scope.histories if row["workspace_id"] == str(workspace_id)]
            return {
                "identity_id": identity_id,
                "workspace_id": workspace_id,
                "account_id": scope.account_id,
                "etag": scope.etag,
                "current_role": scope.current_role.value,
                "ownership": histories[0]["ownership"].value if histories else "unmanaged",
                "local_no_intent": True,
            }

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def review_membership(self, account, *, identity_id, workspace_id, operation, etag, refresh_token, server_ip):
        deadline = time.monotonic() + 45.0
        if operation not in ("release", "adopt"):
            raise AuthTransactionError()

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            scope = self._read_scope(identity_id, workspace_id, source.account_id)
            if scope.etag != etag:
                raise AuthTransactionError("context_changed")

            def guard():
                leases.ensure_owned()
                self._maintenance_guard(scope, source, account, refresh_token, client, owned_deadline)

            leases = CasdoorLeases.for_scopes(client, self._lease_scope(scope, source), deadline=owned_deadline, ttl_seconds=45.0)
            try:
                leases.acquire()
                guard()
                target_role, policy_fingerprint, directory_fingerprint = scope.current_role.value, None, None
                if operation == "adopt":
                    snapshot = self._active()
                    target = self._target_directory(scope, snapshot, owned_deadline, guard)
                    preview = self._preview(snapshot, target.roles, source, uuid4())
                    desired = next(
                        (value for value in preview["targets"] if value["workspace_id"] == str(workspace_id)), None
                    )
                    if desired is None:
                        raise AuthTransactionError("target_mapping_missing")
                    target_role = desired["target_role"]
                    policy_fingerprint, directory_fingerprint = snapshot.policy.proof_fingerprint, target.fingerprint
                else:
                    scoped = [row for row in scope.histories if row["workspace_id"] == str(workspace_id)]
                    if len(scoped) != 1 or scoped[0]["ownership"].value not in ("managed", "local_override"):
                        raise AuthTransactionError("release_unavailable")
                leases.ensure_owned()
                guard()
                handle = secrets.token_urlsafe(32)
                record = {
                    "version": 1,
                    "operation": operation,
                    "identity_id": str(identity_id),
                    "workspace_id": str(workspace_id),
                    "source_account_id": str(source.account_id),
                    "source_refresh_digest": source.refresh_digest,
                    "scope_sha256": scope.fingerprint,
                    "etag": etag,
                    "target_role": target_role,
                    "policy": policy_fingerprint,
                    "directory": directory_fingerprint,
                    "expires_at": time.time() + 60.0,
                }
                if client.set(_REVIEW_PREFIX + handle, canonical(record), ex=60, nx=True) is not True:
                    raise AuthTransactionError("review_unavailable")
                guard()
                return {
                    "review_id": handle,
                    "etag": etag,
                    "operation": operation,
                    "current_role": scope.current_role.value,
                    "target_role": target_role,
                    "expires_in": 60,
                }
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    @staticmethod
    def _consume_review(client, handle):
        if type(handle) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}", handle) is None:
            raise AuthTransactionError()
        raw = client.eval(_CONSUME_REVIEW, 1, _REVIEW_PREFIX + handle)
        try:
            if raw is None or len(raw) > 4096:
                raise ValueError()
            record = json.loads(raw)
            if (
                type(record) is not dict
                or set(record)
                != {
                    "version",
                    "operation",
                    "identity_id",
                    "workspace_id",
                    "source_account_id",
                    "source_refresh_digest",
                    "scope_sha256",
                    "etag",
                    "target_role",
                    "policy",
                    "directory",
                    "expires_at",
                }
                or type(record["version"]) is not int
                or record["version"] != 1
                or type(record["etag"]) is not int
                or record["etag"] < 0
                or record["operation"] not in ("release", "adopt")
                or record["target_role"] not in ("admin", "editor", "normal")
                or type(record["expires_at"]) not in (int, float)
                or not math.isfinite(record["expires_at"])
                or time.time() >= record["expires_at"]
            ):
                raise ValueError()
            for name in ("identity_id", "workspace_id", "source_account_id"):
                if type(record[name]) is not str or str(UUID(record[name])) != record[name]:
                    raise ValueError()
            for name in ("source_refresh_digest", "scope_sha256"):
                if type(record[name]) is not str or re.fullmatch(r"[a-f0-9]{64}", record[name]) is None:
                    raise ValueError()
            for name in ("policy", "directory"):
                value = record[name]
                if record["operation"] == "release":
                    if value is not None:
                        raise ValueError()
                elif type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
                    raise ValueError()
            return record
        except (ValueError, TypeError, KeyError):
            raise AuthTransactionError("review_unavailable") from None

    def release_membership(self, account, *, review_id, etag, refresh_token, server_ip):
        deadline = time.monotonic() + 45.0

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            review = self._consume_review(client, review_id)
            if (
                review["operation"] != "release"
                or review["etag"] != etag
                or review["source_account_id"] != str(source.account_id)
                or review["source_refresh_digest"] != source.refresh_digest
            ):
                raise AuthTransactionError("source_session_invalid")
            scope = self._read_scope(UUID(review["identity_id"]), UUID(review["workspace_id"]), source.account_id)
            if scope.fingerprint != review["scope_sha256"]:
                raise AuthTransactionError("context_changed")

            def guard():
                leases.ensure_owned()
                self._maintenance_guard(scope, source, account, refresh_token, client, owned_deadline)

            leases = CasdoorLeases.for_scopes(client, self._lease_scope(scope, source), deadline=owned_deadline, ttl_seconds=45.0)
            try:
                leases.acquire()
                guard()
                with self._session_factory() as session, session.begin():
                    repository = CasdoorLocalLifecycleRepository(session)
                    locked = repository.inspect(
                        scope.identity_id, scope.workspace_id, source_account_id=source.account_id, lock=True
                    )
                    if locked.fingerprint != scope.fingerprint:
                        raise AuthTransactionError("context_changed")
                    receipt = repository.release(locked, actor_account_id=source.account_id, correlation_id=uuid4())
                    leases.ensure_owned()
                    self._final_refresh(source, refresh_token, client)
                    fresh = CasdoorIdentityLifecycleRepository(session).account(source.account_id)
                    self._configuration_service.require_management(fresh)
                    repository.final_join(locked)
                    self._local_mode()
                    if time.monotonic() >= owned_deadline:
                        raise AuthTransactionError("deadline")
                return {
                    "status": "released",
                    "membership_id": UUID(receipt["membership_id"]),
                    "ownership_epoch": receipt["ownership_epoch"],
                }
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def adopt_membership(self, account, *, review_id, etag, refresh_token, server_ip):
        deadline = time.monotonic() + 45.0

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            review = self._consume_review(client, review_id)
            if (
                review["operation"] != "adopt"
                or review["etag"] != etag
                or review["source_account_id"] != str(source.account_id)
                or review["source_refresh_digest"] != source.refresh_digest
            ):
                raise AuthTransactionError("source_session_invalid")
            scope = self._read_scope(UUID(review["identity_id"]), UUID(review["workspace_id"]), source.account_id)
            if scope.fingerprint != review["scope_sha256"]:
                raise AuthTransactionError("context_changed")

            def guard():
                leases.ensure_owned()
                self._maintenance_guard(scope, source, account, refresh_token, client, owned_deadline)

            leases = CasdoorLeases.for_scopes(client, self._lease_scope(scope, source), deadline=owned_deadline, ttl_seconds=45.0)
            try:
                leases.acquire()
                guard()
                snapshot = self._active()
                target = self._target_directory(scope, snapshot, owned_deadline, guard)
                preview = self._preview(snapshot, target.roles, source, uuid4())
                desired = next(
                    (value for value in preview["targets"] if value["workspace_id"] == str(scope.workspace_id)), None
                )
                if (
                    desired is None
                    or desired["target_role"] != review["target_role"]
                    or target.fingerprint != review["directory"]
                    or snapshot.policy.proof_fingerprint != review["policy"]
                ):
                    raise AuthTransactionError("context_changed")
                leases.ensure_owned()
                guard()
                with self._session_factory() as session, session.begin():
                    repository = CasdoorLocalLifecycleRepository(session)
                    locked = repository.inspect(
                        scope.identity_id, scope.workspace_id, source_account_id=source.account_id, lock=True
                    )
                    if locked.fingerprint != scope.fingerprint:
                        raise AuthTransactionError("context_changed")
                    self._locked_binding(session, snapshot)
                    result = repository.adopt(
                        locked,
                        revision_id=snapshot.binding.revision_id,
                        target_role=desired["target_role"],
                        reason=desired["reason"],
                        actor_account_id=source.account_id,
                        correlation_id=uuid4(),
                    )
                    leases.ensure_owned()
                    repository.final_join(locked, expected_role=TenantAccountRole(desired["target_role"]))
                    self._final_source(session, source, refresh_token, client, snapshot, owned_deadline)
                return {
                    "status": "adopted",
                    "membership_id": UUID(result["membership_id"]),
                    "ownership_epoch": result["ownership_epoch"],
                }
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    def _read_namespace_reset(self, namespace_id, source_account_id):
        with self._session_factory() as session, session.begin():
            return CasdoorLocalLifecycleRepository(session).inspect_namespace_reset(
                namespace_id, source_account_id=source_account_id
            )

    @staticmethod
    def _namespace_reset_leases(scope):
        accounts = tuple(UUID(value) for value in scope["accounts"])
        members = tuple(
            WorkspaceMemberScope(UUID(row["tenant_id"]), UUID(row["account_id"])) for row in (*scope["preserved"]["joins"], *({"tenant_id": r["workspace_id"], "account_id": r["account_id"]} for r in scope["preserved"]["histories"]))
        )
        # Server-derived reset key carries manager/member locks even after self-unlink.
        shared = CasdoorLeaseScope(
            UUID(scope["namespace_id"]), "namespace-reset", account_ids=accounts, members=members
        )
        subjects = tuple(CasdoorLeaseScope(UUID(row["namespace_id"]), row["subject"]) for row in scope["identities"])
        # One namespace key and at most 99 actual subjects in this bounded operation.
        if len(subjects) >= 100:
            raise AuthTransactionError("local_lifecycle_pending")
        return (shared, *subjects)

    def _namespace_reset_guard(self, scope, source, account, refresh_token, client, deadline, leases):
        leases.ensure_owned()
        self._local_mode()
        if time.monotonic() >= deadline or self._source(account, refresh_token, client, management=True) != source:
            raise AuthTransactionError("source_session_invalid")
        if (
            self._read_namespace_reset(UUID(scope["namespace_id"]), source.account_id)["fingerprint"]
            != scope["fingerprint"]
        ):
            raise AuthTransactionError("context_changed")
        leases.ensure_owned()
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")

    def review_namespace_reset(
        self, account, *, namespace_id, etag, confirm_management_review, refresh_token, server_ip
    ):
        deadline = time.monotonic() + 45.0
        if confirm_management_review is not True:
            raise AuthTransactionError("management_review_required")

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            scope = self._read_namespace_reset(namespace_id, source.account_id)
            if scope["etag"] != etag:
                raise AuthTransactionError("context_changed")
            leases = CasdoorLeases.for_scopes(
                client, self._namespace_reset_leases(scope), deadline=owned_deadline, ttl_seconds=45.0
            )
            try:
                leases.acquire()
                self._namespace_reset_guard(scope, source, account, refresh_token, client, owned_deadline, leases)
                handle = secrets.token_urlsafe(32)
                record = {
                    "version": 1,
                    "namespace_id": str(namespace_id),
                    "etag": etag,
                    "fence_epoch": scope["fence_epoch"],
                    "source_account_id": str(source.account_id),
                    "source_refresh_digest": source.refresh_digest,
                    "scope_sha256": scope["fingerprint"],
                    "expires_at": time.time() + 60.0,
                }
                if (
                    client.set("casdoor:namespace-reset-review:v1:" + handle, canonical(record), ex=60, nx=True)
                    is not True
                ):
                    raise AuthTransactionError("review_unavailable")
                self._namespace_reset_guard(scope, source, account, refresh_token, client, owned_deadline, leases)
                return {
                    "review_id": handle,
                    "namespace_id": namespace_id,
                    "etag": etag,
                    "expires_in": 60,
                    "credential_check": "format_only",
                }
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)

    @staticmethod
    def _consume_namespace_reset(client, handle):
        if type(handle) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}", handle) is None:
            raise AuthTransactionError("review_unavailable")
        raw = client.eval(_CONSUME_REVIEW, 1, "casdoor:namespace-reset-review:v1:" + handle)
        try:
            if raw is None or len(raw) > 4096:
                raise ValueError()
            record = json.loads(raw)
            if type(record) is not dict or set(record) != {
                "version",
                "namespace_id",
                "etag",
                "fence_epoch",
                "source_account_id",
                "source_refresh_digest",
                "scope_sha256",
                "expires_at",
            }:
                raise ValueError()
            if type(record["version"]) is not int or record["version"] != 1:
                raise ValueError()
            for key in ("etag", "fence_epoch"):
                if type(record[key]) is not int or not 0 <= record[key] < 9223372036854775807:
                    raise ValueError()
            for key in ("namespace_id", "source_account_id"):
                if type(record[key]) is not str or str(UUID(record[key])) != record[key]:
                    raise ValueError()
            for key in ("source_refresh_digest", "scope_sha256"):
                if type(record[key]) is not str or re.fullmatch(r"[a-f0-9]{64}", record[key]) is None:
                    raise ValueError()
            expiry = record["expires_at"]
            if (
                type(expiry) not in (int, float)
                or not math.isfinite(expiry)
                or not time.time() < expiry <= time.time() + 60.0
            ):
                raise ValueError()
            return record
        except (ValueError, TypeError, KeyError):
            raise AuthTransactionError("review_unavailable") from None

    def reset_namespace(self, account, *, review_id, etag, refresh_token, server_ip):
        deadline = time.monotonic() + 45.0

        def run(client, owned_deadline):
            self._local_mode()
            source = self._source(account, refresh_token, client, management=True)
            review = self._consume_namespace_reset(client, review_id)
            if (
                review["etag"] != etag
                or review["source_account_id"] != str(source.account_id)
                or review["source_refresh_digest"] != source.refresh_digest
            ):
                raise AuthTransactionError("source_session_invalid")
            scope = self._read_namespace_reset(UUID(review["namespace_id"]), source.account_id)
            if scope["fingerprint"] != review["scope_sha256"] or scope["fence_epoch"] != review["fence_epoch"]:
                raise AuthTransactionError("context_changed")
            leases = CasdoorLeases.for_scopes(
                client, self._namespace_reset_leases(scope), deadline=owned_deadline, ttl_seconds=45.0
            )
            try:
                leases.acquire()
                self._namespace_reset_guard(scope, source, account, refresh_token, client, owned_deadline, leases)
                with self._session_factory() as session, session.begin():
                    locked = CasdoorLocalLifecycleRepository(session).inspect_namespace_reset(
                        UUID(scope["namespace_id"]), source_account_id=source.account_id, lock=True
                    )
                    if locked["fingerprint"] != scope["fingerprint"]:
                        raise AuthTransactionError("context_changed")
                    leases.ensure_owned()
                    repository = self._configuration_service._repository(session)
                    leases.ensure_owned()
                    result = repository.reset_namespace(
                        UUID(scope["namespace_id"]),
                        etag=etag,
                        actor_account_id=source.account_id,
                        scope_fingerprint=scope["fingerprint"],
                    )
                    leases.ensure_owned()
                    self._final_refresh(source, refresh_token, client)
                    fresh = CasdoorIdentityLifecycleRepository(session).account(source.account_id)
                    self._configuration_service.require_management(fresh)
                    CasdoorLocalLifecycleRepository(session)._namespace_reset_readback(scope)
                    repository._namespace_reset_last_readback(scope, result)
                    self._local_mode()
                    leases.ensure_owned()
                    if time.monotonic() >= owned_deadline:
                        raise AuthTransactionError("deadline")
                return result
            finally:
                self._release_leases(leases)

        return self._run(RequestAction.START, server_ip, run, deadline=deadline)
