"""Dashboards (saved queries with their charts) and SQL alerts (a saved query checked on a schedule)

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-02 22:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_dashboards',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('description', sa.String(length=2000), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('tiles', sa.Text(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_dashboards_owner', 'cairn_dashboards', ['owner'])
    op.create_table(
        'cairn_sql_alerts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('query', sa.String(length=32), nullable=False),
        sa.Column('measure', sa.String(length=256), nullable=False),
        sa.Column('op', sa.String(length=4), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=False),
        sa.Column('recipients', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('last_value', sa.Float(), nullable=True),
        sa.Column('last_error', sa.String(length=1000), nullable=True),
        sa.Column('last_checked_ms', sa.BigInteger(), nullable=True),
        sa.Column('next_check_ms', sa.BigInteger(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
    )
    op.create_index('ix_cairn_sql_alerts_owner', 'cairn_sql_alerts', ['owner'])
    op.create_index('ix_cairn_sql_alerts_next_check_ms', 'cairn_sql_alerts', ['next_check_ms'])


def downgrade() -> None:
    op.drop_table('cairn_sql_alerts')
    op.drop_table('cairn_dashboards')
