"""Metadata change history with diffs and reverts

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-03 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_change_history',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('aspect', sa.String(length=128), nullable=False),
    sa.Column('actor', sa.String(length=256), nullable=False),
    sa.Column('ts_ms', sa.BigInteger(), nullable=False),
    sa.Column('before', sa.Text(), nullable=True),
    sa.Column('after', sa.Text(), nullable=True),
    sa.Column('after_hash', sa.String(length=40), nullable=False),
    sa.Column('revert_of', sa.String(length=40), nullable=True),
    sa.Column('reverted_by', sa.String(length=40), nullable=True),
    sa.Column('confirmed', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_change_history_urn', 'cairn_change_history', ['urn'], unique=False)
    op.create_index('ix_cairn_change_history_ts_ms', 'cairn_change_history', ['ts_ms'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_change_history_ts_ms', table_name='cairn_change_history')
    op.drop_index('ix_cairn_change_history_urn', table_name='cairn_change_history')
    op.drop_table('cairn_change_history')
