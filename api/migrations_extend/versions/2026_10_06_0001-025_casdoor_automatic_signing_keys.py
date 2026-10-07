"""Allow automatic signing key revisions without changing historical data.

Revision ID: 025_casdoor_auto_signing_keys
Revises: 024_casdoor_avatar_file_guard
Create Date: 2026-10-06 00:01:00.000000

Only the version constraint changes. Secrets, immutable revision digests and
identity bindings remain untouched. Downgrade refuses while any v2 rows exist.
"""

import sqlalchemy as sa
from alembic import op

revision = "025_casdoor_auto_signing_keys"
down_revision = "024_casdoor_avatar_file_guard"
branch_labels = None
depends_on = None

_CONSTRAINT = "casdoor_config_revision_extend_schema_version_check"


def upgrade():
    op.drop_constraint(op.f(_CONSTRAINT), "casdoor_config_revision_extend", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), "casdoor_config_revision_extend", "schema_version IN (1, 2)")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM casdoor_config_revision_extend WHERE schema_version = 2")):
        raise RuntimeError("Automatic Casdoor revisions require the v2-capable schema; downgrade refused")
    op.drop_constraint(op.f(_CONSTRAINT), "casdoor_config_revision_extend", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT), "casdoor_config_revision_extend", "schema_version = 1")
