"""App actions on dashboards, and their runs

Revision ID: 0077
Revises: 0076
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0077'
down_revision = '0076'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bi_actions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('dashboard', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('config', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_bi_actions_dashboard', 'cairn_bi_actions', ['dashboard'])
    op.create_table(
        'cairn_bi_action_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.String(length=500), nullable=False),
        sa.Column('ms', sa.Integer(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_bi_action_runs_action', 'cairn_bi_action_runs', ['action'])


def downgrade() -> None:
    op.drop_table('cairn_bi_action_runs')
    op.drop_table('cairn_bi_actions')
