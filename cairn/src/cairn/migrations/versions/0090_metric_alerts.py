"""Metric alerts: a metric watched over time, its owner told when it goes wrong and when it recovers

Revision ID: 0090
Revises: 0089
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0090'
down_revision = '0089'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_metric_alerts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('metric', sa.String(length=64), nullable=False),
        sa.Column('grain', sa.String(length=10), nullable=False),
        sa.Column('rule', sa.Text(), nullable=False),
        sa.Column('filters', sa.Text(), nullable=False),
        sa.Column('recipients', sa.Text(), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('last_value', sa.Float(), nullable=True),
        sa.Column('last_period', sa.String(length=32), nullable=True),
        sa.Column('last_message', sa.Text(), nullable=True),
        sa.Column('last_checked_ms', sa.BigInteger(), nullable=True),
        sa.Column('next_check_ms', sa.BigInteger(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_metric_alerts_owner', 'cairn_metric_alerts', ['owner'])
    op.create_index('ix_cairn_metric_alerts_metric', 'cairn_metric_alerts', ['metric'])
    op.create_index('ix_cairn_metric_alerts_next_check_ms', 'cairn_metric_alerts', ['next_check_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_metric_alerts_next_check_ms', table_name='cairn_metric_alerts')
    op.drop_index('ix_cairn_metric_alerts_metric', table_name='cairn_metric_alerts')
    op.drop_index('ix_cairn_metric_alerts_owner', table_name='cairn_metric_alerts')
    op.drop_table('cairn_metric_alerts')
