"""Evaluation for agents: evaluation sets and their runs

Revision ID: 0164
Revises: 0163
Create Date: 2026-11-18 05:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0164'
down_revision = '0163'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_eval_sets',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('name', sa.String(64), nullable=False, unique=True),
        sa.Column('owner', sa.String(256), nullable=False),
        sa.Column('description', sa.String(2000), nullable=False),
        sa.Column('cases', sa.Text(), nullable=False),
        sa.Column('scorers', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_eval_sets_owner', 'cairn_eval_sets', ['owner'])
    op.create_table(
        'cairn_eval_runs',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('set_name', sa.String(64), nullable=False),
        sa.Column('set_version', sa.Integer(), nullable=False),
        sa.Column('actor', sa.String(256), nullable=False),
        sa.Column('agent_app', sa.String(64), nullable=False),
        sa.Column('app_version', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('results', sa.Text(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_eval_runs_set_name', 'cairn_eval_runs', ['set_name'])
    op.create_index('ix_cairn_eval_runs_actor', 'cairn_eval_runs', ['actor'])
    op.create_index('ix_cairn_eval_runs_started_ms', 'cairn_eval_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_table('cairn_eval_runs')
    op.drop_table('cairn_eval_sets')
