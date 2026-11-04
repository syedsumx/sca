"""Status page: component checks and incidents

Revision ID: 0072
Revises: 0071
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0072'
down_revision = '0071'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_status_checks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('component', sa.String(length=128), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=500), nullable=True),
    )
    op.create_index('ix_cairn_status_checks_component', 'cairn_status_checks', ['component'])
    op.create_index('ix_cairn_status_checks_ts_ms', 'cairn_status_checks', ['ts_ms'])
    op.create_table(
        'cairn_status_incidents',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('impact', sa.String(length=16), nullable=False),
        sa.Column('components', sa.Text(), nullable=False),
        sa.Column('starts_ms', sa.BigInteger(), nullable=False),
        sa.Column('ends_ms', sa.BigInteger(), nullable=True),
        sa.Column('resolved_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('updates', sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_status_incidents')
    op.drop_table('cairn_status_checks')
