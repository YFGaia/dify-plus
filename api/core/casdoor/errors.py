"""Stable public result codes. Internal exceptions and provider messages stay private."""

from enum import StrEnum


class CasdoorErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    INVALID_TRANSACTION = "invalid_transaction"
    IDENTITY_CONFLICT = "identity_conflict"
    INVITATION_MISMATCH = "invitation_mismatch"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    ROLE_SNAPSHOT_UNKNOWN = "role_snapshot_unknown"
    AUTHORIZATION_PENDING = "authorization_pending"
    REMOTE_ACCOUNT_DISABLED = "remote_account_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CONFIG_CONFLICT = "config_conflict"


class CasdoorDecisionReason(StrEnum):
    """Successful authorization decisions, deliberately separate from error codes."""

    ROLE_MAPPING = "role_mapping"
    DEFAULT_NORMAL_FALLBACK = "default_normal_fallback"
