"""CoWork: a person's scheduled assistant tasks, their runs, and an outbox sent only on approval

Revision ID: 0120
Revises: 0119
Create Date: 2026-11-07 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0120'
down_revision = '0119'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_cowork_tasks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('cron', sa.String(length=100), nullable=False),
        sa.Column('max_steps', sa.Integer(), nullable=False),
        sa.Column('send', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_status', sa.String(length=16), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_cowork_tasks_owner', 'cairn_cowork_tasks', ['owner'])
    op.create_index('ix_cairn_cowork_tasks_next_run_ms', 'cairn_cowork_tasks', ['next_run_ms'])
    op.create_table(
        'cairn_cowork_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('task_id', sa.String(length=32), nullable=False),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('research_id', sa.String(length=40), nullable=True),
        sa.Column('artifact_id', sa.String(length=40), nullable=True),
        sa.Column('outbox_id', sa.String(length=32), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_cowork_runs_task_id', 'cairn_cowork_runs', ['task_id'])
    op.create_table(
        'cairn_cowork_outbox',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('task_id', sa.String(length=32), nullable=True),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('target', sa.Text(), nullable=False),
        sa.Column('subject', sa.String(length=300), nullable=False),
        sa.Column('artifact_id', sa.String(length=40), nullable=False),
        sa.Column('artifact_version', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('decided_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_cowork_outbox_owner', 'cairn_cowork_outbox', ['owner'])


def downgrade() -> None:
    op.drop_index('ix_cairn_cowork_outbox_owner', table_name='cairn_cowork_outbox')
    op.drop_table('cairn_cowork_outbox')
    op.drop_index('ix_cairn_cowork_runs_task_id', table_name='cairn_cowork_runs')
    op.drop_table('cairn_cowork_runs')
    op.drop_index('ix_cairn_cowork_tasks_next_run_ms', table_name='cairn_cowork_tasks')
    op.drop_index('ix_cairn_cowork_tasks_owner', table_name='cairn_cowork_tasks')
    op.drop_table('cairn_cowork_tasks')
