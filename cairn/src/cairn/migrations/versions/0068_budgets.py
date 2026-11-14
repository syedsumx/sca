"""Cost budgets and the alerts they sent

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0068'
down_revision = '0067'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_budgets',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('amount_usd', sa.Float(), nullable=False),
        sa.Column('period', sa.String(length=8), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('thresholds', sa.Text(), nullable=False),
        sa.Column('forecast', sa.Boolean(), nullable=False),
        sa.Column('notify', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('checked_ms', sa.BigInteger(), nullable=True),
        sa.Column('spent_usd', sa.Float(), nullable=True),
    )
    op.create_table(
        'cairn_budget_alerts',
        sa.Column('budget_id', sa.String(length=32), primary_key=True),
        sa.Column('period', sa.String(length=10), primary_key=True),
        sa.Column('kind', sa.String(length=16), primary_key=True),
        sa.Column('sent_ms', sa.BigInteger(), nullable=False),
        sa.Column('spent_usd', sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_budget_alerts')
    op.drop_table('cairn_budgets')
