"""Cairn Flows: flows (draft, sharing, schedule state), their versions, runs and step runs

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-09 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0132'
down_revision = '0131'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_flows',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('editors', sa.Text(), nullable=False),
        sa.Column('viewers', sa.Text(), nullable=False),
        sa.Column('draft', sa.Text(), nullable=False),
        sa.Column('draft_errors', sa.Integer(), nullable=False),
        sa.Column('published_version', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('watch', sa.Text(), nullable=False),
        sa.Column('follows', sa.Text(), nullable=False),
        sa.Column('seen_change', sa.BigInteger(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.UniqueConstraint('name'),
    )
    op.create_index('ix_cairn_flows_owner', 'cairn_flows', ['owner'])
    op.create_index('ix_cairn_flows_next_run_ms', 'cairn_flows', ['next_run_ms'])
    op.create_table(
        'cairn_flow_versions',
        sa.Column('flow_id', sa.String(length=32), primary_key=True),
        sa.Column('version', sa.Integer(), primary_key=True),
        sa.Column('definition', sa.Text(), nullable=False),
        sa.Column('published_by', sa.String(length=256), nullable=False),
        sa.Column('published_ms', sa.BigInteger(), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
    )
    op.create_table(
        'cairn_flow_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('flow_id', sa.String(length=32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('definition', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('triggered_by', sa.String(length=256), nullable=True),
        sa.Column('run_as', sa.String(length=256), nullable=False),
        sa.Column('params', sa.Text(), nullable=False),
        sa.Column('logical_date', sa.String(length=10), nullable=True),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=True),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('wake_ms', sa.BigInteger(), nullable=True),
        sa.Column('lease_owner', sa.String(length=128), nullable=True),
        sa.Column('lease_until_ms', sa.BigInteger(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('sla_alerted', sa.Boolean(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_flow_runs_status', 'cairn_flow_runs', ['status'])
    op.create_index('ix_cairn_flow_runs_flow_created', 'cairn_flow_runs', ['flow_id', 'created_ms'])
    op.create_table(
        'cairn_flow_step_runs',
        sa.Column('run_id', sa.String(length=32), primary_key=True),
        sa.Column('key', sa.String(length=140), primary_key=True),
        sa.Column('step', sa.String(length=64), nullable=False),
        sa.Column('parent', sa.String(length=64), nullable=True),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=True),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('next_ms', sa.BigInteger(), nullable=True),
        sa.Column('output', sa.Text(), nullable=True),
        sa.Column('log', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('state', sa.Text(), nullable=True),
        sa.Column('decision', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_flow_step_runs')
    op.drop_index('ix_cairn_flow_runs_flow_created', table_name='cairn_flow_runs')
    op.drop_index('ix_cairn_flow_runs_status', table_name='cairn_flow_runs')
    op.drop_table('cairn_flow_runs')
    op.drop_table('cairn_flow_versions')
    op.drop_index('ix_cairn_flows_next_run_ms', table_name='cairn_flows')
    op.drop_index('ix_cairn_flows_owner', table_name='cairn_flows')
    op.drop_table('cairn_flows')
