"""Data residency: the region a share's recipient and a reverse sync's destination keep data in

Revision ID: 0104
Revises: 0103
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0104'
down_revision = '0103'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_lakehouse_shares') as b:
        b.add_column(sa.Column('region', sa.String(length=32), nullable=True))
    with op.batch_alter_table('cairn_reverse_syncs') as b:
        b.add_column(sa.Column('region', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_reverse_syncs') as b:
        b.drop_column('region')
    with op.batch_alter_table('cairn_lakehouse_shares') as b:
        b.drop_column('region')
