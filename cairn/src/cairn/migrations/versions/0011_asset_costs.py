"""Daily storage and compute cost per dataset

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-28 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_asset_costs',
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('platform', sa.String(length=64), nullable=False),
    sa.Column('storage_bytes', sa.BigInteger(), nullable=True),
    sa.Column('storage_usd', sa.Float(), nullable=False),
    sa.Column('compute_usd', sa.Float(), nullable=False),
    sa.Column('reads', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('urn', 'day')
    )
    with op.batch_alter_table('cairn_asset_costs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_asset_costs_platform'), ['platform'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_asset_costs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_asset_costs_platform'))
    op.drop_table('cairn_asset_costs')
