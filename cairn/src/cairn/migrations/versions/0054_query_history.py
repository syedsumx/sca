"""Query history: each finished Trino query with its statistics, for its author and operators

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0054'
down_revision = '0053'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_query_history',
        sa.Column('query_id', sa.String(length=64), primary_key=True),
        sa.Column('user', sa.String(length=256), nullable=False),
        sa.Column('source', sa.String(length=256), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('error_code', sa.String(length=128), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('sql', sa.Text(), nullable=False),
        sa.Column('create_ms', sa.BigInteger(), nullable=False),
        sa.Column('end_ms', sa.BigInteger(), nullable=True),
        sa.Column('wall_ms', sa.BigInteger(), nullable=False),
        sa.Column('cpu_ms', sa.BigInteger(), nullable=False),
        sa.Column('queued_ms', sa.BigInteger(), nullable=False),
        sa.Column('input_bytes', sa.BigInteger(), nullable=False),
        sa.Column('input_rows', sa.BigInteger(), nullable=False),
        sa.Column('output_rows', sa.BigInteger(), nullable=False),
        sa.Column('peak_memory_bytes', sa.BigInteger(), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('plan', sa.Text(), nullable=True),
        sa.Column('server', sa.String(length=256), nullable=False),
        sa.Column('splits', sa.Integer(), nullable=False),
    )
    op.create_index('ix_cairn_query_history_user', 'cairn_query_history', ['user'])
    op.create_index('ix_cairn_query_history_state', 'cairn_query_history', ['state'])
    op.create_index('ix_cairn_query_history_create_ms', 'cairn_query_history', ['create_ms'])


def downgrade() -> None:
    op.drop_table('cairn_query_history')
