"""recommended list sorted by usage frequency

Revision ID: 001_recommended_list_sorted
Revises: 
Create Date: 2024-07-25 14:55:34.357214

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

import models as models

# revision identifiers, used by Alembic.
revision = '001_recommended_list_sorted'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()
    
    if 'app_statistics_extend' not in tables:
        op.create_table('app_statistics_extend',
        sa.Column('id', models.types.StringUUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('app_id', models.types.StringUUID(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='app_statistics_extend_pkey')
        )
        with op.batch_alter_table('app_statistics_extend', schema=None) as batch_op:
            batch_op.create_index('app_statistics_extend_app_id_idx', ['app_id'], unique=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('app_statistics_extend', schema=None) as batch_op:
        batch_op.drop_index('app_statistics_extend_app_id_idx')

    op.drop_table('app_statistics_extend')
    # ### end Alembic commands ###
