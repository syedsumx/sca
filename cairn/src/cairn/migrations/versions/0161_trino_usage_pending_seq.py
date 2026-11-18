"""Trino usage: a waiting bucket's seq, so a write to Core never marks a bucket written that changed meanwhile

Revision ID: 0161
Revises: 0160
Create Date: 2026-11-18 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0161'
down_revision = '0160'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_trino_usage_pending') as b:
        b.add_column(sa.Column('seq', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_trino_usage_pending') as b:
        b.drop_column('seq')
