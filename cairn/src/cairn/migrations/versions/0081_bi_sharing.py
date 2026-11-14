"""Sharing BI assets (saved queries, workbooks, dashboards) with people and groups

Revision ID: 0081
Revises: 0080
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0081'
down_revision = '0080'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bi_shares',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.Column('principal', sa.String(length=256), nullable=False),
        sa.Column('role', sa.String(length=8), nullable=False),
        sa.Column('granted_by', sa.String(length=256), nullable=False),
        sa.Column('granted_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'asset_id', 'principal'),
    )
    op.create_index('ix_cairn_bi_shares_principal', 'cairn_bi_shares', ['principal', 'kind'])


def downgrade() -> None:
    op.drop_index('ix_cairn_bi_shares_principal', table_name='cairn_bi_shares')
    op.drop_table('cairn_bi_shares')
