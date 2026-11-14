"""Run costs: each finished Spark run, priced once (cores x wall-clock time) and charged to what it wrote

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-02 23:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_run_costs',
        sa.Column('run', sa.String(length=128), primary_key=True),
        sa.Column('parent', sa.String(length=128), nullable=True),
        sa.Column('job', sa.String(length=512), nullable=False),
        sa.Column('engine', sa.String(length=32), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('cores', sa.Float(), nullable=False),
        sa.Column('seconds', sa.Float(), nullable=False),
        sa.Column('usd', sa.Float(), nullable=False),
        sa.Column('outputs', sa.String(length=4000), nullable=False),
    )
    op.create_index('ix_cairn_run_costs_parent', 'cairn_run_costs', ['parent'])
    op.create_index('ix_cairn_run_costs_job', 'cairn_run_costs', ['job'])
    op.create_index('ix_cairn_run_costs_day', 'cairn_run_costs', ['day'])


def downgrade() -> None:
    op.drop_table('cairn_run_costs')
