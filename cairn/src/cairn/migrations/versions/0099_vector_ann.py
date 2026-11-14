"""Approximate (IVF) search settings for vector indexes

Revision ID: 0099
Revises: 0098
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0099'
down_revision = '0098'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_vector_indexes') as t:
        t.add_column(sa.Column('ann_lists', sa.Integer(), nullable=True))
        t.add_column(sa.Column('ann_probes', sa.Integer(), nullable=True))
        t.add_column(sa.Column('ann_snapshot', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_vector_indexes') as t:
        t.drop_column('ann_snapshot')
        t.drop_column('ann_probes')
        t.drop_column('ann_lists')
