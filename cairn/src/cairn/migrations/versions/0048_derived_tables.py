"""Derived tables: a SELECT kept as a lakehouse table, refreshed incrementally

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_derived_tables',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=256), nullable=False, unique=True),
        sa.Column('sql', sa.Text(), nullable=False),
        sa.Column('target', sa.String(length=512), nullable=False, unique=True),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('watermark', sa.String(length=128), nullable=True),
        sa.Column('key', sa.String(length=1024), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=True),
        sa.Column('on_change', sa.Boolean(), nullable=False),
        sa.Column('upstreams', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('seen_change', sa.BigInteger(), nullable=False),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('lease_until', sa.BigInteger(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_derived_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('derived_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=False),
        sa.Column('error', sa.String(length=2000), nullable=True),
    )
    op.create_index('ix_cairn_derived_runs_derived_id', 'cairn_derived_runs', ['derived_id'])
    op.create_index('ix_cairn_derived_runs_started_ms', 'cairn_derived_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_derived_runs_started_ms', table_name='cairn_derived_runs')
    op.drop_index('ix_cairn_derived_runs_derived_id', table_name='cairn_derived_runs')
    op.drop_table('cairn_derived_runs')
    op.drop_table('cairn_derived_tables')
