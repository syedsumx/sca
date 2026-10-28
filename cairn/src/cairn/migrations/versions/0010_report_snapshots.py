"""Daily governance and adoption report snapshots

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-28 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_report_snapshots',
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('data', sa.Text(), nullable=False),
    sa.Column('computed_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('day')
    )


def downgrade() -> None:
    op.drop_table('cairn_report_snapshots')
