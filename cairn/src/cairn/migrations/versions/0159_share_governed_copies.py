"""Shares: governed copies (tables with masks or row filters, shared as copies with them applied)

Revision ID: 0159
Revises: 0158
Create Date: 2026-11-17 07:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0159'
down_revision = '0158'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('cairn_lakehouse_shares', sa.Column('governed', sa.Text(), nullable=True))
    op.add_column('cairn_lakehouse_shares', sa.Column('refresh_minutes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('cairn_lakehouse_shares', 'refresh_minutes')
    op.drop_column('cairn_lakehouse_shares', 'governed')
