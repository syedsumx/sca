"""Managed NiFi: when the lakehouse token a flow's NiFi holds was issued (Cairn replaces it half way through its life)

Revision ID: 0146
Revises: 0145
Create Date: 2026-11-12 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0146'
down_revision = '0145'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_nifi_flows') as b:
        b.add_column(sa.Column('lake_token_s', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_nifi_flows') as b:
        b.drop_column('lake_token_s')
