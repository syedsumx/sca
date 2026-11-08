"""Code sandboxes' runs

Revision ID: 0130
Revises: 0127
Create Date: 2026-11-08 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0130'
down_revision = '0127'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_sandbox_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('agent', sa.String(length=64), nullable=True),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('network', sa.Text(), nullable=False),
        sa.Column('timeout_s', sa.Integer(), nullable=False),
        sa.Column('memory_mb', sa.Integer(), nullable=False),
        sa.Column('cpus', sa.Float(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('input_bytes', sa.BigInteger(), nullable=False),
        sa.Column('output_bytes', sa.BigInteger(), nullable=True),
        sa.Column('egress_bytes', sa.BigInteger(), nullable=True),
        sa.Column('files', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_sandbox_runs_actor', 'cairn_sandbox_runs', ['actor'])
    op.create_index('ix_cairn_sandbox_runs_started_ms', 'cairn_sandbox_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_sandbox_runs_started_ms', table_name='cairn_sandbox_runs')
    op.drop_index('ix_cairn_sandbox_runs_actor', table_name='cairn_sandbox_runs')
    op.drop_table('cairn_sandbox_runs')
