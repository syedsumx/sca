"""Shares: registered model versions downloaded by a share's recipient

Revision ID: 0163
Revises: 0162
Create Date: 2026-11-17 17:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0163'
down_revision = '0162'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_share_models',
        sa.Column('share_id', sa.String(32), nullable=False),
        sa.Column('model', sa.String(256), nullable=False),
        sa.Column('version', sa.String(16), nullable=False),
        sa.Column('attached_by', sa.String(256), nullable=False),
        sa.Column('attached_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('share_id', 'model', 'version'),
    )
    op.create_index('ix_cairn_share_models_share_id', 'cairn_share_models', ['share_id'])


def downgrade() -> None:
    op.drop_index('ix_cairn_share_models_share_id', table_name='cairn_share_models')
    op.drop_table('cairn_share_models')
