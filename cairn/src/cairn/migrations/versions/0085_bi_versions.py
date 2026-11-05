"""BI version history: every save of a saved query, workbook or dashboard

Revision ID: 0085
Revises: 0084
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0085'
down_revision = '0084'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bi_versions',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('saved_by', sa.String(length=512), nullable=False),
        sa.Column('saved_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'asset_id', 'version'),
    )


def downgrade() -> None:
    op.drop_table('cairn_bi_versions')
