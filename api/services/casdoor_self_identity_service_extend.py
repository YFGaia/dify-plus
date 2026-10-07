"""Historical self observations, never authorization, finalization or online proof.

Native admission may update last-active/current-workspace bookkeeping before this
service. This new reader uses a separate SELECT-only, non-autoflushing session.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from core.casdoor.ownership import parse_local_withdrawal_json, parse_role_baseline_json
from libs.datetime_utils import utc_now
from libs.helper import email as validate_email
from machinery.context import RequestContext
from repositories.casdoor_profile_repository_extend import _name, _parse_time, _snapshot
from repositories.casdoor_self_identity_repository_extend import (
    CasdoorSelfIdentityRepository,
    CasdoorSelfReadConflict,
)
from sqlalchemy.exc import SQLAlchemyError

from services.account_email import normalize_email

ROLES = {"owner", "admin", "editor", "normal", "dataset_operator"}


def _uuid(value):
    try:
        return value if type(value) is str and str(UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _generation(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _safe_organization(value):
    try:
        return _name(value)
    except (ValueError, TypeError, UnicodeError):
        return None


def _activity(namespace):
    if (
        not namespace
        or not _generation(namespace["fence_epoch"])
        or not namespace["integration_present"]
        or namespace["slot"] != 1
        or type(namespace["enabled"]) is not int
        or namespace["enabled"] not in (0, 1)
    ):
        return "unknown"
    if namespace["lifecycle"] in ("archived", "fencing") or namespace["enabled"] == 0:
        return "inactive"
    if (
        namespace["lifecycle"] != "active"
        or namespace["active_revision_present"] is None
        or namespace["active_integration_id"] != namespace["integration_id"]
    ):
        return "unknown"
    # Different historical namespaces legitimately have different core values.
    # Their binding is inactive; only this namespace's active revision can be
    # compared with its own scalar core association, without loading config.
    if namespace["active_namespace_id"] != namespace["id"]:
        return "inactive"
    if all(
        namespace[key] is True
        for key in (
            "active_issuer_matches",
            "active_organization_matches",
            "active_application_matches",
            "active_client_matches",
        )
    ):
        return "active"
    return "unknown"


def _profile(row, account):
    result = {
        "name": {
            "last_status": None,
            "last_reason": None,
            "last_sync_at": None,
            "recorded_generation": None,
            "baseline_generation": None,
            "current_local_differs_from_last_applied": None,
        },
        "email": {"current_differs": None, "verified": None, "last_status": None, "last_differs": None},
        "profile_consistency": "unknown",
    }
    if not _generation(row["sync_generation"]):
        return result
    try:
        baseline = _snapshot(row["last_applied_json"], baseline=True, generation=row["sync_generation"])
        sync = _snapshot(row["profile_sync_json"], baseline=False, generation=row["sync_generation"])
        name = result["name"]
        if "name" in baseline:
            name["baseline_generation"] = baseline["name_generation"]
            if account["name"] is not None:
                name["current_local_differs_from_last_applied"] = _name(account["name"]) != baseline["name"]
        if sync:
            name.update(
                last_status=sync["name_status"],
                last_reason=sync["name_reason"],
                last_sync_at=sync["last_sync_at"],
                recorded_generation=sync["generation"],
            )
            email = result["email"]
            email.update(last_status=sync["remote_email_status"], last_differs=sync["remote_email_differs"])
            # INVALID/UNAVAILABLE snapshots can retain an older email in storage.
            # They never turn that retained value into a fresh remote observation.
            if sync["remote_email_status"] in ("same", "different") and row["email_verified"] in (None, 0, 1):
                try:
                    remote = validate_email(row["remote_email"])
                    local = validate_email(account["email"])
                    email["current_differs"] = normalize_email(remote) != normalize_email(local)
                    email["verified"] = None if row["email_verified"] is None else bool(row["email_verified"])
                except (ValueError, TypeError, AttributeError):
                    pass
        result["profile_consistency"] = (
            "historical" if sync and sync["generation"] < row["sync_generation"] else "consistent"
        )
    except (ValueError, TypeError, UnicodeError):
        pass
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _desired(raw):
    """Closed persisted schema-1 desired metadata, never actual role authority."""
    if type(raw) is not str or len(raw.encode("utf-8")) > 65535:
        raise ValueError()
    value = json.loads(raw, object_pairs_hook=_pairs)
    required = {"schema_version", "backend", "target_role", "builtin_id", "role_ids", "fence_epoch"}
    if (
        type(value) is not dict
        or set(value) not in (required, required | {"reason"})
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["backend"] not in ("local", "remote")
        or value["target_role"] not in ("admin", "editor", "normal")
        or type(value["builtin_id"]) is not str
        or not value["builtin_id"]
        or value["role_ids"] != [value["builtin_id"]]
        or not _generation(value["fence_epoch"])
        or ("reason" in value and value["reason"] not in ("role_mapping", "default_normal_fallback"))
        or json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != raw
    ):
        raise ValueError()
    if value["backend"] == "local" and value["builtin_id"] != value["target_role"]:
        raise ValueError()
    return value


def _join(row):
    joins = row["joins"]
    if len(joins) > 1:
        return None, "unknown"
    if not joins:
        return None, "absent"
    join = joins[0]
    if not _uuid(join["id"]) or not join["workspace_present"] or join["role"] not in ROLES:
        return None, "unknown"
    return join, "present"


def _membership(row, rbac_enabled):
    join, presence = _join(row)
    result = {
        "id": _uuid(row["id"]),
        "workspace_id": None,
        "namespace_id": None,
        "identity_id": None,
        "recorded_ownership": row["ownership"]
        if row["ownership"] in ("managed", "released", "local_override")
        else None,
        "recorded_source": row["source"] if row["source"] in ("mapping", "fallback", "adopt") else None,
        "recorded_finalization": row["finalization"]
        if row["finalization"] in ("pending", "finalized", "manual_recovery")
        else None,
        "tombstone": bool(row["tombstone"]) if row["tombstone"] in (0, 1) else None,
        "join_presence": presence,
        "local_role": join["role"] if join else None,
        "state": "unknown",
        "consistency": "unknown",
        "remote_actual_state": "unknown",
    }
    ns = row["namespace"]
    observation = row.get("_archived_observation")
    tracked = (
        type(observation) is tuple
        and len(observation) == 3
        and observation[0] == row["id"]
        and observation[1] in ("current", "released")
        and type(observation[2]) is str
        and len(observation[2]) == 64
        and all(c in "0123456789abcdef" for c in observation[2])
    )
    if (
        not ns
        or not ns["integration_present"]
        or ns["slot"] != 1
        or not _generation(ns["fence_epoch"])
        or any(not _uuid(row[k]) for k in ("id", "workspace_id", "namespace_id", "identity_id", "revision_id"))
        or not row["workspace_present"]
        or row["foreign_identity"]
        or row["revision_namespace"] != row["namespace_id"]
        or row["revision_integration"] != ns["integration_id"]
        or not all(
            row[k] is True
            for k in (
                "revision_issuer_matches",
                "revision_organization_matches",
                "revision_application_matches",
                "revision_client_matches",
            )
        )
        or (
            row["own_identity"] is not None
            and (
                row["identity_namespace"] != row["namespace_id"]
                or not row["identity_issuer_matches"]
                or not row["identity_organization_matches"]
            )
        )
    ):
        return result
    result.update(workspace_id=row["workspace_id"], namespace_id=row["namespace_id"], identity_id=row["own_identity"])
    if (
        len(row["scope_refs"]) != 1
        or (len(row["history_refs"]) != 1 and not tracked)
        or presence == "unknown"
        or not _generation(row["desired_generation"])
        or not _generation(row["ownership_epoch"])
        or any(
            result[k] is None for k in ("recorded_ownership", "recorded_source", "recorded_finalization", "tombstone")
        )
    ):
        return result
    try:
        baseline = parse_role_baseline_json(row["baseline_json"])
        applied = parse_role_baseline_json(row["last_applied_roles_json"])
        if hashlib.sha256(row["last_applied_roles_json"].encode()).hexdigest() != row["last_applied_fingerprint"]:
            raise ValueError()
        if baseline.backend != applied.backend:
            raise ValueError()
        if tracked and observation[1] == "released":
            if (
                row["ownership"] != "released"
                or result["tombstone"]
                or row["finalization"] != "finalized"
                or applied.backend.value != "local"
            ):
                raise ValueError()
            result.update(state="unmanaged", consistency="historical")
            return result
        try:
            marker = parse_local_withdrawal_json(row["desired_roles_json"])
        except ValueError:
            marker = None
        desired = marker if marker else _desired(row["desired_roles_json"])
        if desired["backend"] != applied.backend.value or desired["fence_epoch"] != ns["fence_epoch"]:
            result["consistency"] = "stale"
            return result
        if row["own_identity"] is not None and row["identity_generation"] != row["desired_generation"]:
            result["consistency"] = "stale"
            return result
        result["consistency"] = "historical"
        if result["tombstone"]:
            result["state"] = "tombstone"
        elif row["ownership"] == "local_override":
            result["state"] = "local_override"
        elif row["ownership"] == "released":
            result["state"] = "unmanaged"
        elif marker:
            if (
                presence != "absent"
                or row["removed_present"]
                or marker["removed_join_id"] != row["join_id"]
                or marker["withdrawal_epoch"] != row["ownership_epoch"]
                or marker["withdrawal_generation"] != row["desired_generation"]
                or applied.backend.value != "local"
                or applied.join_role is not None
                or applied.roles
            ):
                raise ValueError()
            result["state"] = "controlled_withdrawal"
        elif presence == "absent":
            result["state"] = "absent_unknown"
        elif join["id"] != row["join_id"] or join["role"] == "owner":
            result.update(state="unknown", consistency="unknown")
        elif row["own_identity"] is None or _activity(ns) != "active":
            result["state"] = "historical"
        elif rbac_enabled or applied.backend.value != "local":
            result["state"] = "recorded_managed"
        elif applied.join_role != join["role"]:
            result["state"] = "local_override"
        else:
            result["state"] = "recorded_managed"
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        result.update(state="unknown", consistency="unknown")
    return result


class CasdoorSelfIdentityService:
    def __init__(self, *, session_factory, rbac_enabled: bool, now: Callable[[], datetime] = utc_now):
        self._session_factory = session_factory
        self._rbac_enabled = rbac_enabled
        self._now = now

    def get_avatar_observations(self, context: RequestContext, *, now, identity_after=None, limit=20):
        """Read the same private observations independently of membership pages.

        This reads real durable SQL facts; it cannot synchronize, dispatch, retry
        or prove storage completion. Caller supplies an aware UTC clock value.
        """
        if (
            not _uuid(context.account_id)
            or type(limit) is not int
            or not 1 <= limit <= 50
            or type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
        ):
            raise CasdoorSelfReadConflict()
        try:
            with self._session_factory(autoflush=False) as session, session.no_autoflush:
                reader = CasdoorSelfIdentityRepository(session, context.account_id)
                account = reader.avatar_account()
                identities, more, cursor = reader.identities(identity_after, limit)
                result = {
                    "identities": [
                        {"id": row["id"], **self._avatar(reader.avatar_observation(row, account), now)}
                        for row in identities
                    ],
                    "identity_has_more": more,
                    "identity_next": cursor,
                }
                reader.recheck()
                return result
        except SQLAlchemyError:
            raise CasdoorSelfReadConflict() from None

    @staticmethod
    def _avatar(facts, now):
        """Return only a closed observation; DB readback is never a synced result."""
        result = {
            "avatar_status": "unknown",
            "avatar_recorded_at": None,
            "avatar_last_reason": None,
            "avatar_recorded_generation": None,
            "avatar_consistency": "unknown",
            "avatar_current_local_differs_from_last_applied": None,
        }
        if facts is None:
            return result
        row = facts["intent"]
        result["avatar_consistency"] = "current" if facts["current"] else "historical"
        if row is None:
            result["avatar_status"] = ("no_record" if facts["enabled"] else "off") if facts["current"] else "historical"
            return result
        state = row["operation_state"]
        result["avatar_recorded_generation"] = row["generation"]
        result["avatar_current_local_differs_from_last_applied"] = facts["local_differs"]
        if state in ("applied", "failed"):
            recorded_at = (
                row["readback_at"]
                if state == "applied" or facts.get("cleanup_complete") is True
                else row["terminated_at"]
            )
        elif state == "unknown":
            recorded_at = row["updated_at"]
        else:
            recorded_at = row["created_at"]
        result["avatar_recorded_at"] = recorded_at.replace(tzinfo=UTC).isoformat(timespec="microseconds")
        result["avatar_last_reason"] = row["error_code"]
        if not facts["current"]:
            result["avatar_status"] = "historical"
        elif state == "applied":
            result["avatar_status"] = "local_override" if facts["local_differs"] else "local_attachment_recorded"
        elif state == "failed":
            result["avatar_status"] = (
                "failed_storage_cleaned" if facts.get("cleanup_complete") is True else "failed_before_storage"
            )
        elif state == "pending":
            result["avatar_status"] = (
                "source_expired" if _parse_time(facts["data"]["url_expires_at"]) <= now else "pending"
            )
        elif state == "in_flight":
            result["avatar_status"] = "in_flight"
        return result

    def get(
        self,
        context: RequestContext,
        *,
        identity_after=None,
        membership_after=None,
        current_membership_after=None,
        limit=20,
    ):
        if not _uuid(context.account_id) or type(limit) is not int or not 1 <= limit <= 50:
            raise CasdoorSelfReadConflict()
        now = self._now()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise CasdoorSelfReadConflict()
        with self._session_factory(autoflush=False) as session, session.no_autoflush:
            reader = CasdoorSelfIdentityRepository(session, context.account_id)
            account = reader.account()
            avatar_account = reader.avatar_account()
            linked = reader.linked()
            identities, identity_more, identity_next = reader.identities(identity_after, limit)
            history, membership_more, membership_next = reader.memberships(membership_after, limit)
            current, current_more, current_next = reader.current_memberships(current_membership_after, limit)
            result = {
                "binding": "linked" if linked else "unlinked",
                "identities": [],
                "memberships": [_membership(row, self._rbac_enabled) for row in history],
                "current_memberships": [],
                "identity_has_more": identity_more,
                "identity_next": identity_next,
                "membership_has_more": membership_more,
                "membership_next": membership_next,
                "current_membership_has_more": current_more,
                "current_membership_next": current_next,
                "actions": {
                    key: False for key in ("link", "unlink", "reauthenticate", "logout", "adopt", "release", "retry")
                },
            }
            for row in identities:
                ns = row["namespace"]
                valid = ns and row["issuer_matches"] and row["organization_matches"] and _uuid(row["namespace_id"])
                organization = _safe_organization(row["organization"]) if valid else None
                valid = valid and organization is not None
                result["identities"].append(
                    {
                        "id": _uuid(row["id"]),
                        "namespace_id": row["namespace_id"] if valid else None,
                        "organization": organization,
                        "masked_identifier": "********",
                        "activity": _activity(ns) if valid else "unknown",
                        "lifecycle": ns["lifecycle"]
                        if valid and ns["lifecycle"] in ("active", "fencing", "archived")
                        else "unknown",
                        "sync_generation": row["sync_generation"] if _generation(row["sync_generation"]) else None,
                        **self._avatar(reader.avatar_observation(row, avatar_account) if valid else None, now),
                        **_profile(row if valid else {**row, "sync_generation": None}, account),
                    }
                )
            for row in current:
                join, presence = _join(row)
                result["current_memberships"].append(
                    {
                        "id": _uuid(row["id"]),
                        "workspace_id": _uuid(row["workspace_id"]),
                        "join_presence": presence,
                        "local_role": join["role"] if join else None,
                        "state": "unknown"
                        if presence != "present"
                        else ("history_present" if row["history_refs"] else "unmanaged"),
                        "remote_actual_state": "unknown",
                    }
                )
            reader.recheck()
            return result
