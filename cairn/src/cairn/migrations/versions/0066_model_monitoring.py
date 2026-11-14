"""Model monitoring: monitors, the call log of served models, and checks

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0066'
down_revision = '0065'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_ml_monitors',
        sa.Column('model', sa.String(length=256), primary_key=True),
        sa.Column('baseline', sa.Text(), nullable=False),
        sa.Column('features', sa.Text(), nullable=True),
        sa.Column('drift_threshold', sa.Float(), nullable=False),
        sa.Column('min_rows', sa.Integer(), nullable=False),
        sa.Column('window_minutes', sa.Integer(), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=False),
        sa.Column('log_payloads', sa.Boolean(), nullable=False),
        sa.Column('retention_days', sa.Integer(), nullable=False),
        sa.Column('notify', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('profile', sa.Text(), nullable=True),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
    )
    op.create_table(
        'cairn_ml_inferences',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('model', sa.String(length=256), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('caller', sa.String(length=256), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('rows', sa.Integer(), nullable=True),
        sa.Column('inputs', sa.Text(), nullable=True),
        sa.Column('predictions', sa.Text(), nullable=True),
        sa.Column('labels', sa.Text(), nullable=True),
        sa.Column('labelled_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_ml_inferences_model', 'cairn_ml_inferences', ['model'])
    op.create_index('ix_cairn_ml_inferences_ts_ms', 'cairn_ml_inferences', ['ts_ms'])
    op.create_table(
        'cairn_ml_monitor_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('model', sa.String(length=256), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('window_from_ms', sa.BigInteger(), nullable=False),
        sa.Column('rows', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('metrics', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_ml_monitor_runs_model', 'cairn_ml_monitor_runs', ['model'])


def downgrade() -> None:
    op.drop_table('cairn_ml_monitor_runs')
    op.drop_table('cairn_ml_inferences')
    op.drop_table('cairn_ml_monitors')
