"""Sample datasets for new tenants: which sets are loaded or removed, and by whom

Revision ID: 0101
Revises: 0100
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0101'
down_revision = '0100'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_sample_sets',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_sample_sets')
