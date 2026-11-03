"""SQL workflows: scheduled steps (derived-table refreshes, checks, reports) and their runs

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0056'
down_revision = '0055'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_sql_workflows',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('cron', sa.String(length=128), nullable=True),
        sa.Column('recipients', sa.Text(), nullable=False),
        sa.Column('notify', sa.String(length=16), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_status', sa.String(length=16), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_sql_workflows_owner', 'cairn_sql_workflows', ['owner'])
    op.create_index('ix_cairn_sql_workflows_next_run_ms', 'cairn_sql_workflows', ['next_run_ms'])
    op.create_table(
        'cairn_sql_workflow_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('workflow_id', sa.String(length=32), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('steps', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_sql_workflow_runs_workflow_id', 'cairn_sql_workflow_runs', ['workflow_id'])


def downgrade() -> None:
    op.drop_table('cairn_sql_workflow_runs')
    op.drop_table('cairn_sql_workflows')
