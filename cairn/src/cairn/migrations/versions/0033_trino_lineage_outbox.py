"""Trino lineage held while Cairn Core is unavailable

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-01 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_trino_lineage_outbox',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('events', sa.Text(), nullable=False),
    sa.Column('received_ms', sa.BigInteger(), nullable=False),
    sa.Column('next_ms', sa.BigInteger(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_trino_lineage_outbox_next_ms', 'cairn_trino_lineage_outbox', ['next_ms'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_trino_lineage_outbox_next_ms', table_name='cairn_trino_lineage_outbox')
    op.drop_table('cairn_trino_lineage_outbox')
