"""Trino usage: a waiting bucket's next try, so a bucket Core refuses never holds up the others

Revision ID: 0165
Revises: 0164
Create Date: 2026-11-18 14:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0165'
down_revision = '0164'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_trino_usage_pending') as b:
        b.add_column(sa.Column('next_ms', sa.BigInteger(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_trino_usage_pending') as b:
        b.drop_column('next_ms')
