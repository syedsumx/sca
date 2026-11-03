"""Online feature store: served feature tables and their values

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0065'
down_revision = '0064'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_online_feature_tables',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('timestamp_column', sa.String(length=128), nullable=True),
        sa.Column('ttl_seconds', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('types', sa.Text(), nullable=True),
        sa.Column('synced_ms', sa.BigInteger(), nullable=True),
        sa.Column('source_snapshot', sa.BigInteger(), nullable=True),
        sa.Column('entities', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
    )
    op.create_table(
        'cairn_online_feature_values',
        sa.Column('feature_table', sa.String(length=64), primary_key=True),
        sa.Column('entity', sa.String(length=64), primary_key=True),
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('vals', sa.Text(), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('event_ms', sa.BigInteger(), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_online_feature_values')
    op.drop_table('cairn_online_feature_tables')
