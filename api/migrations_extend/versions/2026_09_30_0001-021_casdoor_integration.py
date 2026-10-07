"""Add independent Casdoor configuration, identity and durable synchronization storage.

Revision ID: 021_casdoor_integration
Revises: 020_workflow_run_account
Create Date: 2026-09-30 00:01:00.000000

No seed rows or changes to legacy provider data. Application rollback requires
stopping producers and proving remote in-flight effects terminated before this
schema is removed; dropping tables is not a remote permission rollback.
"""

import sqlalchemy as sa
from alembic import op
from models.types import LongText, StringUUID
from sqlalchemy.dialects.mysql import VARCHAR as MYSQL_VARCHAR

revision = "021_casdoor_integration"
down_revision = "020_workflow_run_account"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "casdoor_integration_extend",
        sa.Column("slot", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_revision_id", StringUUID(), nullable=True),
        sa.Column("draft_revision_id", StringUUID(), nullable=True),
        sa.Column("etag", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_by", StringUUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("etag >= 0", name=op.f("casdoor_integration_extend_etag_nn_check")),
        sa.PrimaryKeyConstraint("id", name="casdoor_integration_extend_pkey"),
        sa.CheckConstraint("slot = 1", name=op.f("casdoor_integration_extend_singleton_slot_check")),
        sa.UniqueConstraint("slot", name="casdoor_integration_slot_key"),
    )
    op.create_table(
        "casdoor_namespace_extend",
        sa.Column("integration_id", StringUUID(), nullable=False),
        sa.Column("expected_issuer", sa.Text(), nullable=False),
        sa.Column("organization", sa.String(255), nullable=False),
        sa.Column("application", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("core_fingerprint", sa.String(64), nullable=False),
        sa.Column("lifecycle", sa.String(24), nullable=False, server_default=sa.text("'active'")),
        sa.Column("fence_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("fence_epoch >= 0", name=op.f("casdoor_namespace_extend_fence_epoch_nn_check")),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["casdoor_integration_extend.id"],
            name="casdoor_namespace_extend_integration_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'fencing', 'archived')", name=op.f("casdoor_namespace_extend_lifecycle_check")
        ),
        sa.PrimaryKeyConstraint("id", name="casdoor_namespace_extend_pkey"),
    )
    op.create_index(
        "casdoor_namespace_integration_lifecycle_idx", "casdoor_namespace_extend", ["integration_id", "lifecycle"]
    )
    op.create_table(
        "casdoor_config_revision_extend",
        sa.Column("integration_id", StringUUID(), nullable=False),
        sa.Column("namespace_id", StringUUID(), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("browser_frontend_url", sa.Text(), nullable=False),
        sa.Column("backend_api_url", sa.Text(), nullable=False),
        sa.Column("expected_issuer", sa.Text(), nullable=False),
        sa.Column("organization", sa.String(255), nullable=False),
        sa.Column("application", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("button_text", sa.String(120), nullable=False),
        sa.Column("default_workspace_id", StringUUID(), nullable=False),
        sa.Column("encrypted_secret", LongText(), nullable=True),
        sa.Column("certificates_json", sa.Text(), nullable=False),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("mappings_json", LongText(), nullable=False),
        sa.Column("created_by", StringUUID(), nullable=True),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["casdoor_integration_extend.id"],
            name="casdoor_config_revision_extend_integration_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["casdoor_namespace_extend.id"],
            name="casdoor_config_revision_extend_namespace_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="casdoor_config_revision_extend_pkey"),
        sa.CheckConstraint(
            "revision_number > 0", name=op.f("casdoor_config_revision_extend_revision_number_positive_check")
        ),
        sa.CheckConstraint("schema_version = 1", name=op.f("casdoor_config_revision_extend_schema_version_check")),
        sa.UniqueConstraint("integration_id", "revision_number", name="casdoor_revision_number_key"),
    )
    op.create_index("casdoor_revision_namespace_idx", "casdoor_config_revision_extend", ["namespace_id"])
    op.create_table(
        "casdoor_validation_extend",
        sa.Column("revision_id", StringUUID(), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("rbac_mode", sa.String(8), nullable=True),
        sa.Column("proof_fingerprint", sa.String(64), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("actor_account_id", StringUUID(), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint(
            "kind IN ('static', 'protocol', 'diagnostic', 'deployment')",
            name=op.f("casdoor_validation_extend_kind_check"),
        ),
        sa.PrimaryKeyConstraint("id", name="casdoor_validation_extend_pkey"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["casdoor_config_revision_extend.id"],
            name="casdoor_validation_extend_revision_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'passed', 'failed', 'unknown')", name=op.f("casdoor_validation_extend_status_check")
        ),
    )
    op.create_index(
        "casdoor_validation_revision_kind_idx", "casdoor_validation_extend", ["revision_id", "kind", "checked_at"]
    )
    op.create_table(
        "casdoor_identity_extend",
        sa.Column("namespace_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("organization", sa.String(255), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "subject_digest",
            sa.String(64).with_variant(MYSQL_VARCHAR(64, charset="ascii", collation="ascii_bin"), "mysql"),
            nullable=False,
        ),
        sa.Column("remote_email", sa.String(255), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("sync_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("remote_profile_version", sa.String(128), nullable=True),
        sa.Column("last_applied_json", sa.Text(), nullable=False),
        sa.Column("profile_sync_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("namespace_id", "account_id", name="casdoor_identity_account_key"),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["casdoor_namespace_extend.id"],
            name="casdoor_identity_extend_namespace_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="casdoor_identity_extend_pkey"),
        sa.CheckConstraint("sync_generation >= 0", name=op.f("casdoor_identity_extend_sync_generation_nn_check")),
        sa.UniqueConstraint("namespace_id", "subject_digest", name="casdoor_identity_subject_key"),
    )
    op.create_table(
        "casdoor_managed_membership_extend",
        sa.Column("namespace_id", StringUUID(), nullable=False),
        sa.Column("identity_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("workspace_id", StringUUID(), nullable=False),
        sa.Column("join_id", StringUUID(), nullable=True),
        sa.Column("ownership", sa.String(24), nullable=False),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("desired_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("revision_id", StringUUID(), nullable=False),
        sa.Column("last_applied_roles_json", sa.Text(), nullable=False),
        sa.Column("last_applied_fingerprint", sa.String(64), nullable=True),
        sa.Column("desired_roles_json", sa.Text(), nullable=False),
        sa.Column("baseline_json", sa.Text(), nullable=False),
        sa.Column("finalization", sa.String(24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("tombstone", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint(
            "desired_generation >= 0", name=op.f("casdoor_managed_membership_extend_desired_generation_nn_check")
        ),
        sa.CheckConstraint(
            "finalization IN ('pending', 'finalized', 'manual_recovery')",
            name=op.f("casdoor_managed_membership_extend_finalization_check"),
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["casdoor_namespace_extend.id"],
            name="casdoor_managed_membership_extend_namespace_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ownership IN ('managed', 'local_override', 'released')",
            name=op.f("casdoor_managed_membership_extend_ownership_check"),
        ),
        sa.CheckConstraint(
            "ownership_epoch >= 0", name=op.f("casdoor_managed_membership_extend_ownership_epoch_nn_check")
        ),
        sa.PrimaryKeyConstraint("id", name="casdoor_managed_membership_extend_pkey"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["casdoor_config_revision_extend.id"],
            name="casdoor_managed_membership_extend_revision_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source IN ('mapping', 'fallback', 'adopt')", name=op.f("casdoor_managed_membership_extend_source_check")
        ),
        sa.UniqueConstraint("namespace_id", "account_id", "workspace_id", name="casdoor_membership_scope_key"),
    )
    op.create_index("casdoor_membership_identity_idx", "casdoor_managed_membership_extend", ["identity_id"])
    op.create_index("casdoor_membership_join_idx", "casdoor_managed_membership_extend", ["join_id"])
    op.create_table(
        "casdoor_sync_intent_extend",
        sa.Column("namespace_id", StringUUID(), nullable=False),
        sa.Column("identity_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("workspace_id", StringUUID(), nullable=True),
        sa.Column("membership_id", StringUUID(), nullable=True),
        sa.Column("revision_id", StringUUID(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("fence_epoch", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(24), nullable=True),
        sa.Column("resource_id", StringUUID(), nullable=True),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("desired_json", sa.Text(), nullable=False),
        sa.Column("operation_state", sa.String(24), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("termination_state", sa.String(24), nullable=False, server_default=sa.text("'not_started'")),
        sa.Column("attempt_id", StringUUID(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("readback_at", sa.DateTime(), nullable=True),
        sa.Column("terminated_at", sa.DateTime(), nullable=True),
        sa.Column("termination_proof_kind", sa.String(32), nullable=True),
        sa.Column("proof_ref", sa.String(128), nullable=True),
        sa.Column("retry_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("idempotency_key", name="casdoor_intent_idempotency_key"),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("casdoor_sync_intent_extend_attempt_count_nn_check")),
        sa.CheckConstraint("fence_epoch >= 0", name=op.f("casdoor_sync_intent_extend_fence_epoch_nn_check")),
        sa.CheckConstraint("generation >= 0", name=op.f("casdoor_sync_intent_extend_generation_nn_check")),
        sa.CheckConstraint(
            "kind IN ('role_replace', 'member_remove', 'resource_grant', 'resource_revoke', "
            "'invitation_finalize', 'profile_avatar')",
            name=op.f("casdoor_sync_intent_extend_kind_check"),
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["casdoor_managed_membership_extend.id"],
            name="casdoor_sync_intent_extend_membership_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["casdoor_namespace_extend.id"],
            name="casdoor_sync_intent_extend_namespace_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "operation_state IN ('pending', 'in_flight', 'unknown', 'applied', 'failed', 'cancelled')",
            name=op.f("casdoor_sync_intent_extend_operation_state_check"),
        ),
        sa.CheckConstraint("ownership_epoch >= 0", name=op.f("casdoor_sync_intent_extend_ownership_epoch_nn_check")),
        sa.PrimaryKeyConstraint("id", name="casdoor_sync_intent_extend_pkey"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["casdoor_config_revision_extend.id"],
            name="casdoor_sync_intent_extend_revision_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "termination_state IN ('not_started', 'unconfirmed', 'confirmed', 'manual_recovery')",
            name=op.f("casdoor_sync_intent_extend_termination_state_check"),
        ),
    )
    op.create_index("casdoor_intent_membership_idx", "casdoor_sync_intent_extend", ["membership_id"])
    op.create_index("casdoor_intent_pending_retry_idx", "casdoor_sync_intent_extend", ["operation_state", "retry_at"])
    op.create_index(
        "casdoor_intent_scope_generation_idx",
        "casdoor_sync_intent_extend",
        ["namespace_id", "scope_digest", "generation"],
    )
    op.create_table(
        "casdoor_audit_extend",
        sa.Column("namespace_id", StringUUID(), nullable=True),
        sa.Column("revision_id", StringUUID(), nullable=True),
        sa.Column("identity_id", StringUUID(), nullable=True),
        sa.Column("account_id", StringUUID(), nullable=True),
        sa.Column("actor_account_id", StringUUID(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result_code", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.PrimaryKeyConstraint("id", name="casdoor_audit_extend_pkey"),
    )
    op.create_index("casdoor_audit_correlation_idx", "casdoor_audit_extend", ["correlation_id"])
    op.create_index("casdoor_audit_namespace_created_idx", "casdoor_audit_extend", ["namespace_id", "created_at"])


def downgrade():
    op.drop_table("casdoor_audit_extend")
    op.drop_table("casdoor_sync_intent_extend")
    op.drop_table("casdoor_managed_membership_extend")
    op.drop_table("casdoor_identity_extend")
    op.drop_table("casdoor_validation_extend")
    op.drop_table("casdoor_config_revision_extend")
    op.drop_table("casdoor_namespace_extend")
    op.drop_table("casdoor_integration_extend")
