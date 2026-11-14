"""Reverse ETL syncs and the hashes of the rows they sent

Revision ID: 0095
Revises: 0094
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0095'
down_revision = '0094'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_reverse_syncs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('source', sa.String(length=130), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('object', sa.String(length=80), nullable=False),
        sa.Column('key_column', sa.String(length=128), nullable=False),
        sa.Column('key_field', sa.String(length=80), nullable=False),
        sa.Column('mapping', sa.Text(), nullable=False),
        sa.Column('secret_scope', sa.String(length=128), nullable=False),
        sa.Column('cron', sa.String(length=100), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('running', sa.Boolean(), nullable=False),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_status', sa.String(length=10), nullable=True),
        sa.Column('last_sent', sa.Integer(), nullable=True),
        sa.Column('last_failed', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_reverse_syncs_owner', 'cairn_reverse_syncs', ['owner'])
    op.create_index('ix_cairn_reverse_syncs_next_run_ms', 'cairn_reverse_syncs', ['next_run_ms'])
    op.create_table(
        'cairn_reverse_sync_rows',
        sa.Column('sync_id', sa.String(length=32), primary_key=True),
        sa.Column('key_hash', sa.String(length=64), primary_key=True),
        sa.Column('row_hash', sa.String(length=64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_reverse_sync_rows')
    op.drop_index('ix_cairn_reverse_syncs_next_run_ms', table_name='cairn_reverse_syncs')
    op.drop_index('ix_cairn_reverse_syncs_owner', table_name='cairn_reverse_syncs')
    op.drop_table('cairn_reverse_syncs')
