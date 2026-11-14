"""Connector syncs (L165): open connector images (the Airbyte protocol) into Iceberg

Revision ID: 0154
Revises: 0153
Create Date: 2026-11-14 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0154'
down_revision = '0153'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_connector_syncs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('image', sa.String(length=512), nullable=False),
        sa.Column('config', sa.Text(), nullable=False),
        sa.Column('secret_scope', sa.String(length=128), nullable=True),
        sa.Column('streams', sa.Text(), nullable=False),
        sa.Column('egress', sa.Text(), nullable=False),
        sa.Column('limits', sa.Text(), nullable=True),
        sa.Column('cron', sa.String(length=100), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('state', sa.Text(), nullable=True),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('running', sa.Boolean(), nullable=False),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_status', sa.String(length=10), nullable=True),
        sa.Column('last_rows', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_stats', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_connector_syncs_owner', 'cairn_connector_syncs', ['owner'])
    op.create_index('ix_cairn_connector_syncs_next_run_ms', 'cairn_connector_syncs', ['next_run_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_connector_syncs_next_run_ms', table_name='cairn_connector_syncs')
    op.drop_index('ix_cairn_connector_syncs_owner', table_name='cairn_connector_syncs')
    op.drop_table('cairn_connector_syncs')
