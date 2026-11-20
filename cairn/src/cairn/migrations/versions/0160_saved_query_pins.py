"""The saved query text each person approved for unattended runs as them (alerts, SQL workflows, flows)

Revision ID: 0160
Revises: 0159
Create Date: 2026-11-17 00:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0160'
down_revision = '0159'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_saved_query_pins',
        sa.Column('principal', sa.String(length=256), primary_key=True),
        sa.Column('query_id', sa.String(length=32), primary_key=True),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('pinned_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_saved_query_pins')
