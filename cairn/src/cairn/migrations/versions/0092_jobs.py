"""Scheduled notebook and Python jobs and their runs

Revision ID: 0092
Revises: 0091
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0092'
down_revision = '0091'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_jobs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('params', sa.Text(), nullable=False),
        sa.Column('cron', sa.String(length=100), nullable=True),
        sa.Column('timeout_minutes', sa.Integer(), nullable=False),
        sa.Column('recipients', sa.Text(), nullable=False),
        sa.Column('notify', sa.String(length=10), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_jobs_owner', 'cairn_jobs', ['owner'])
    op.create_index('ix_cairn_jobs_next_run_ms', 'cairn_jobs', ['next_run_ms'])
    op.create_table(
        'cairn_job_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('job_id', sa.String(length=32), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('as_user', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('output', sa.Text(), nullable=True),
        sa.Column('log', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_job_runs_job_id', 'cairn_job_runs', ['job_id'])
    op.create_index('ix_cairn_job_runs_started_ms', 'cairn_job_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_job_runs_started_ms', table_name='cairn_job_runs')
    op.drop_index('ix_cairn_job_runs_job_id', table_name='cairn_job_runs')
    op.drop_table('cairn_job_runs')
    op.drop_index('ix_cairn_jobs_next_run_ms', table_name='cairn_jobs')
    op.drop_index('ix_cairn_jobs_owner', table_name='cairn_jobs')
    op.drop_table('cairn_jobs')
