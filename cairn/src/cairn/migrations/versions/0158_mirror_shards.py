"""Mirrors with several ordered logs (DynamoDB Streams' shards): where each was read to

Revision ID: 0158
Revises: 0157
Create Date: 2026-11-14 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0158'
down_revision = '0157'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_mirror_shards',
        sa.Column('mirror_id', sa.String(length=32), primary_key=True),
        sa.Column('shard', sa.String(length=128), primary_key=True),
        sa.Column('position', sa.String(length=128), nullable=True),
        sa.Column('done', sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_mirror_shards')
