"""Bad-row quarantine: pipes and streams set aside the rows a table refuses

Revision ID: 0103
Revises: 0102
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0103'
down_revision = '0102'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_pipes') as b:
        b.add_column(sa.Column('bad_rows', sa.String(length=16), nullable=False, server_default='fail'))
    with op.batch_alter_table('cairn_pipe_files') as b:
        b.add_column(sa.Column('rejected', sa.Integer(), nullable=True))
    with op.batch_alter_table('cairn_streams') as b:
        b.add_column(sa.Column('bad_rows', sa.String(length=16), nullable=False, server_default='fail'))
    op.create_table(
        'cairn_pipe_rejects',
        sa.Column('pipe_id', sa.String(length=32), primary_key=True),
        sa.Column('key', sa.String(length=1024), primary_key=True),
        sa.Column('version', sa.String(length=64), primary_key=True),
        sa.Column('row', sa.Integer(), primary_key=True),
        sa.Column('reason', sa.String(length=1000), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('rejected_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_pipe_rejects_rejected_ms', 'cairn_pipe_rejects', ['rejected_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_pipe_rejects_rejected_ms', table_name='cairn_pipe_rejects')
    op.drop_table('cairn_pipe_rejects')
    with op.batch_alter_table('cairn_streams') as b:
        b.drop_column('bad_rows')
    with op.batch_alter_table('cairn_pipe_files') as b:
        b.drop_column('rejected')
    with op.batch_alter_table('cairn_pipes') as b:
        b.drop_column('bad_rows')
