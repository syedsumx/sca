"""Scheduled updates: the update policy and the plans (L160)

Revision ID: 0149
Revises: 0148
Create Date: 2026-11-12 19:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0149'
down_revision = '0148'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_update_policy',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('policy', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.String(512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_update_plans',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('from_version', sa.String(64), nullable=False),
        sa.Column('target', sa.String(64), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('severity', sa.String(16), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('auto', sa.Boolean(), nullable=False),
        sa.Column('not_before_ms', sa.BigInteger(), nullable=True),
        sa.Column('requested_by', sa.String(512), nullable=False),
        sa.Column('approved_by', sa.String(512), nullable=True),
        sa.Column('job', sa.String(128), nullable=True),
        sa.Column('log', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=True),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_update_plans_status', 'cairn_update_plans', ['status'])


def downgrade() -> None:
    op.drop_index('ix_cairn_update_plans_status', 'cairn_update_plans')
    op.drop_table('cairn_update_plans')
    op.drop_table('cairn_update_policy')
