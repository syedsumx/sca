"""SQL workspace: each person's query history and saved (optionally shared) queries with a chart

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-02 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_sql_history',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('connection', sa.String(length=256), nullable=False),
        sa.Column('sql', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('rows', sa.Integer(), nullable=True),
        sa.Column('ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=1000), nullable=True),
        sa.Column('at_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_sql_history_actor', 'cairn_sql_history', ['actor'])
    op.create_index('ix_cairn_sql_history_at_ms', 'cairn_sql_history', ['at_ms'])
    op.create_table(
        'cairn_saved_queries',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('description', sa.String(length=2000), nullable=False),
        sa.Column('connection', sa.String(length=256), nullable=False),
        sa.Column('sql', sa.Text(), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('chart', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_saved_queries_owner', 'cairn_saved_queries', ['owner'])


def downgrade() -> None:
    op.drop_table('cairn_saved_queries')
    op.drop_table('cairn_sql_history')
