"""Lakehouse table maintenance: policies (optimize, expire snapshots, remove orphan files) and their runs

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-02 23:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_maintenance_policies',
        sa.Column('scope', sa.String(length=512), primary_key=True),
        sa.Column('optimize', sa.Boolean(), nullable=False),
        sa.Column('optimize_threshold', sa.String(length=16), nullable=False),
        sa.Column('expire_days', sa.Integer(), nullable=True),
        sa.Column('orphan_days', sa.Integer(), nullable=True),
        sa.Column('every_hours', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_maintenance_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('table_name', sa.String(length=512), nullable=False),
        sa.Column('operation', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_maintenance_runs_table_name', 'cairn_maintenance_runs', ['table_name'])
    op.create_index('ix_cairn_maintenance_runs_started_ms', 'cairn_maintenance_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_table('cairn_maintenance_runs')
    op.drop_table('cairn_maintenance_policies')
