"""BI lineage: the tables and columns each saved query, workbook and dashboard reads

Revision ID: 0084
Revises: 0083
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0084'
down_revision = '0083'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bi_lineage',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.Column('dataset', sa.String(length=700), nullable=False),
        sa.Column('column', sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'asset_id', 'dataset', 'column'),
    )
    op.create_index('ix_cairn_bi_lineage_dataset', 'cairn_bi_lineage', ['dataset', 'column'])


def downgrade() -> None:
    op.drop_index('ix_cairn_bi_lineage_dataset', table_name='cairn_bi_lineage')
    op.drop_table('cairn_bi_lineage')
