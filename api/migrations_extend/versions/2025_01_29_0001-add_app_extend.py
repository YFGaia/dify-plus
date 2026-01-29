"""add_app_extend

Revision ID: 013_app_extend
Revises: 012_account_money_extend_unique
Create Date: 2025-01-29

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

from models import types

# revision identifiers, used by Alembic.
revision = '013_app_extend'
down_revision = '012_account_money_extend_unique'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'app_extend' not in tables:
        op.create_table('app_extend',
            sa.Column('id', types.StringUUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
            sa.Column('app_id', types.StringUUID(), nullable=False),
            sa.Column('retention_number', sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint('id', name='app_extend_joins_pkey')
        )
        with op.batch_alter_table('app_extend', schema=None) as batch_op:
            batch_op.create_index('app_extend_id_app_id_idx', ['app_id'], unique=False)


def downgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'app_extend' in tables:
        with op.batch_alter_table('app_extend', schema=None) as batch_op:
            batch_op.drop_index('app_extend_id_app_id_idx')
        op.drop_table('app_extend')
