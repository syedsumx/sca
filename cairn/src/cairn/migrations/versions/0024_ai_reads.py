"""AI usage lineage: datasets read by agents, MCP sessions and the assistant, per day

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-31 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_ai_reads',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('agent', sa.String(length=40), nullable=True),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('channel', sa.String(length=12), nullable=False),
    sa.Column('dataset', sa.String(length=700), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('last_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_ai_reads_agent', 'cairn_ai_reads', ['agent'], unique=False)
    op.create_index('ix_cairn_ai_reads_dataset', 'cairn_ai_reads', ['dataset'], unique=False)
    op.create_index('ix_cairn_ai_reads_day', 'cairn_ai_reads', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_ai_reads_day', table_name='cairn_ai_reads')
    op.drop_index('ix_cairn_ai_reads_dataset', table_name='cairn_ai_reads')
    op.drop_index('ix_cairn_ai_reads_agent', table_name='cairn_ai_reads')
    op.drop_table('cairn_ai_reads')
