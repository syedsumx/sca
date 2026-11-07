"""Aggregation and projection policies: one-query identities for queries Cairn checked against them

Revision ID: 0112
Revises: 0111
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0112'
down_revision = '0111'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_checked_queries',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('groups', sa.Text(), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_checked_queries_expires_ms', 'cairn_checked_queries', ['expires_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_checked_queries_expires_ms', table_name='cairn_checked_queries')
    op.drop_table('cairn_checked_queries')
