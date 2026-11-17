"""Shares: stored functions' definitions read by a share's recipient

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-17 18:00:00.000000
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
        'cairn_share_functions',
        sa.Column('share_id', sa.String(32), nullable=False),
        sa.Column('function', sa.String(512), nullable=False),
        sa.Column('statements', sa.Text(), nullable=False),
        sa.Column('attached_by', sa.String(256), nullable=False),
        sa.Column('attached_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('share_id', 'function'),
    )
    op.create_index('ix_cairn_share_functions_share_id', 'cairn_share_functions', ['share_id'])


def downgrade() -> None:
    op.drop_index('ix_cairn_share_functions_share_id', table_name='cairn_share_functions')
    op.drop_table('cairn_share_functions')
