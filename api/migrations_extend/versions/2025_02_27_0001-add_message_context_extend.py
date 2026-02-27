"""add_message_context_extend

Revision ID: 014_message_context_extend
Revises: 013_app_extend
Create Date: 2025-02-27

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

from models import types

# revision identifiers, used by Alembic.
revision = '014_message_context_extend'
down_revision = '013_app_extend'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'message_context_extend' not in tables:
        op.create_table('message_context_extend',
            sa.Column('id', types.StringUUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False),
            sa.Column('conversation_id', sa.String(36), nullable=True),
            sa.Column('message_id', sa.String(36), nullable=False),
            sa.PrimaryKeyConstraint('id', name='message_context_extend_joins_pkey')
        )
        with op.batch_alter_table('message_context_extend', schema=None) as batch_op:
            batch_op.create_index('message_context_conversation_id_idx', ['conversation_id'], unique=False)
            batch_op.create_index('message_context_created_at_idx', ['created_at'], unique=False)


def downgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'message_context_extend' in tables:
        with op.batch_alter_table('message_context_extend', schema=None) as batch_op:
            batch_op.drop_index('message_context_conversation_id_idx')
            batch_op.drop_index('message_context_created_at_idx')
        op.drop_table('message_context_extend')
