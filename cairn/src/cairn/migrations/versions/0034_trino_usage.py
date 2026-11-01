"""Usage statistics from Trino queries

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-01 15:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_trino_usage',
    sa.Column('day_ms', sa.BigInteger(), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('user', sa.String(length=256), nullable=False),
    sa.Column('field', sa.String(length=256), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('day_ms', 'urn', 'user', 'field')
    )
    op.create_table('cairn_trino_usage_queries',
    sa.Column('query_id', sa.String(length=64), nullable=False),
    sa.Column('day_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('query_id')
    )
    op.create_index('ix_cairn_trino_usage_queries_day_ms', 'cairn_trino_usage_queries', ['day_ms'], unique=False)
    op.create_table('cairn_trino_usage_pending',
    sa.Column('day_ms', sa.BigInteger(), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.PrimaryKeyConstraint('day_ms', 'urn')
    )


def downgrade() -> None:
    op.drop_table('cairn_trino_usage_pending')
    op.drop_index('ix_cairn_trino_usage_queries_day_ms', table_name='cairn_trino_usage_queries')
    op.drop_table('cairn_trino_usage_queries')
    op.drop_table('cairn_trino_usage')
