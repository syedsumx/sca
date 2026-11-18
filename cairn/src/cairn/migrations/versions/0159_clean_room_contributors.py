"""Clean rooms: who brought each table, so every run re-checks that they still read it whole

Revision ID: 0159
Revises: 0158
Create Date: 2026-11-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0159'
down_revision = '0158'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_clean_rooms') as t:
        t.add_column(sa.Column('contributors', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_clean_rooms') as t:
        t.drop_column('contributors')
