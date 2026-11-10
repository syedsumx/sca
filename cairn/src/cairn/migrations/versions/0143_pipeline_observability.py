"""Pipeline observability: the runs of Airflow DAGs, flows, dbt, jobs and OpenLineage pipelines

Revision ID: 0143
Revises: 0141
Create Date: 2026-11-10 22:00:00.000000

(Data products, pipeline SLAs and OTLP exporters are kept in cairn_obs_config with the other observability
definitions; no table of their own.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0143'
down_revision = '0141'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_obs_pipeline_runs',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('pipeline', sa.String(length=300), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('instance', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=250), nullable=False),
        sa.Column('ol_run_id', sa.String(length=64), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('start_ms', sa.BigInteger(), nullable=True),
        sa.Column('end_ms', sa.BigInteger(), nullable=True),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('trace_id', sa.String(length=32), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('inputs', sa.Text(), nullable=False),
        sa.Column('outputs', sa.Text(), nullable=False),
        sa.Column('tasks', sa.Text(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('emitted', sa.Boolean(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pipeline', 'run_id', name='uq_cairn_obs_pipeline_runs_run'),
    )
    op.create_index('ix_cairn_obs_pipeline_runs_ol_run_id', 'cairn_obs_pipeline_runs', ['ol_run_id'])
    op.create_index('ix_cairn_obs_pipeline_runs_state', 'cairn_obs_pipeline_runs', ['state'])
    op.create_index('ix_cairn_obs_pipeline_runs_end_ms', 'cairn_obs_pipeline_runs', ['end_ms'])
    op.create_index('ix_cairn_obs_pipeline_runs_updated_ms', 'cairn_obs_pipeline_runs', ['updated_ms'])
    op.create_index('ix_cairn_obs_pipeline_runs_pipeline_start', 'cairn_obs_pipeline_runs', ['pipeline', 'start_ms'])


def downgrade() -> None:
    op.drop_table('cairn_obs_pipeline_runs')
