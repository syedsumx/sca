"""Governed volumes and their grants

Revision ID: 0093
Revises: 0092
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0093'
down_revision = '0092'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_volumes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=40), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('bucket', sa.String(length=63), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_volumes_owner', 'cairn_volumes', ['owner'])
    op.create_table(
        'cairn_volume_grants',
        sa.Column('volume_id', sa.String(length=32), primary_key=True),
        sa.Column('principal', sa.String(length=256), primary_key=True),
        sa.Column('access', sa.String(length=5), nullable=False),
        sa.Column('granted_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_volume_grants')
    op.drop_index('ix_cairn_volumes_owner', table_name='cairn_volumes')
    op.drop_table('cairn_volumes')
