"""Persist the initialization workspace for global configuration authorization.

Revision ID: 026_system_management_scope
Revises: 025_casdoor_auto_signing_keys

Backfill only a unique earliest, normal workspace corroborated by the install
record and an initialized owner from the same installation interval. Uncertain
history leaves the singleton empty (access denied); authorization never retries
an election. Names, IdP defaults and deployment account lists are not evidence.
"""

from datetime import timedelta

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "026_system_management_scope"
down_revision = "025_casdoor_auto_signing_keys"
branch_labels = None
depends_on = None


def _initialization_tenant_id(bind):
    tenants = sa.table(
        "tenants", sa.column("id", StringUUID()), sa.column("created_at", sa.DateTime()), sa.column("status")
    )
    setups = sa.table("dify_setups", sa.column("setup_at", sa.DateTime()))
    accounts = sa.table(
        "accounts",
        sa.column("id", StringUUID()),
        sa.column("initialized_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
    )
    joins = sa.table(
        "tenant_account_joins",
        sa.column("tenant_id", StringUUID()),
        sa.column("account_id", StringUUID()),
        sa.column("role"),
    )
    earliest_at = bind.scalar(sa.select(sa.func.min(tenants.c.created_at)))
    setup_at = bind.scalar(sa.select(sa.func.min(setups.c.setup_at)))
    if earliest_at is None or setup_at is None:
        return None
    earliest = bind.execute(sa.select(tenants.c.id, tenants.c.status).where(tenants.c.created_at == earliest_at)).all()
    if len(earliest) != 1 or earliest[0].status != "normal":
        return None
    tenant_id = earliest[0].id
    # SQLite/MySQL may truncate server timestamp precision. Compare seconds,
    # while allowing a bounded setup interval rather than requiring equal times.
    tenant_time, setup_time = (value.replace(microsecond=0) for value in (earliest_at, setup_at))
    owners = bind.execute(
        sa.select(accounts.c.created_at, accounts.c.initialized_at)
        .select_from(joins.join(accounts, accounts.c.id == joins.c.account_id))
        .where(joins.c.tenant_id == tenant_id, joins.c.role == "owner")
    ).all()
    if len(owners) != 1 or owners[0].initialized_at is None:
        return None
    created_time = owners[0].created_at.replace(microsecond=0)
    initialized_time = owners[0].initialized_at.replace(microsecond=0)
    # Application initialized_at and database server defaults can straddle a
    # second boundary (including rounding). Admit only this bounded clock skew.
    skew = timedelta(seconds=1)
    if (
        created_time > initialized_time + skew
        or initialized_time > tenant_time + skew
        or tenant_time > setup_time + skew
    ):
        return None
    if setup_time - initialized_time > timedelta(hours=1):
        return None
    return tenant_id


def upgrade():
    op.create_table(
        "system_management_scope_extend",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("tenant_id", StringUUID(), nullable=False),
        sa.CheckConstraint("id = 'initialization'", name="system_management_scope_singleton"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    bind = op.get_bind()
    tenant_id = _initialization_tenant_id(bind)
    if tenant_id is not None:
        scope = sa.table(
            "system_management_scope_extend", sa.column("id", sa.String(32)), sa.column("tenant_id", StringUUID())
        )
        bind.execute(scope.insert().values(id="initialization", tenant_id=tenant_id))


def downgrade():
    op.drop_table("system_management_scope_extend")
