"""Chargeback: cost centres and monthly statements, frozen when issued

Revision ID: 0107
Revises: 0106
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0107'
down_revision = '0106'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_cost_centres',
        sa.Column('code', sa.String(length=64), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('teams', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_chargeback_statements',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('month', sa.String(length=7), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('issued_by', sa.String(length=256), nullable=False),
        sa.Column('issued_ms', sa.BigInteger(), nullable=False),
        sa.Column('total_usd', sa.Text(), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_chargeback_statements_month', 'cairn_chargeback_statements', ['month'])


def downgrade() -> None:
    op.drop_index('ix_cairn_chargeback_statements_month', table_name='cairn_chargeback_statements')
    op.drop_table('cairn_chargeback_statements')
    op.drop_table('cairn_cost_centres')
