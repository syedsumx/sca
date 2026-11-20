"""Shares: volumes (folders of files) read by a share's recipient

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-17 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0162'
down_revision = '0161'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_share_volumes',
        sa.Column('share_id', sa.String(32), nullable=False),
        sa.Column('volume', sa.String(40), nullable=False),
        sa.Column('prefix', sa.String(1024), nullable=False),
        sa.Column('attached_by', sa.String(256), nullable=False),
        sa.Column('attached_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('share_id', 'volume', 'prefix'),
    )
    op.create_index('ix_cairn_share_volumes_share_id', 'cairn_share_volumes', ['share_id'])
    op.create_index('ix_cairn_share_volumes_volume', 'cairn_share_volumes', ['volume'])


def downgrade() -> None:
    op.drop_index('ix_cairn_share_volumes_volume', table_name='cairn_share_volumes')
    op.drop_index('ix_cairn_share_volumes_share_id', table_name='cairn_share_volumes')
    op.drop_table('cairn_share_volumes')
