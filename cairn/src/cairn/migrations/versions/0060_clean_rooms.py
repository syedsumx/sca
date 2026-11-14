"""Clean rooms: parties join their tables in aggregate, through the room's own identity

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0060'
down_revision = '0059'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_clean_rooms',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('parties', sa.Text(), nullable=False),
        sa.Column('min_group', sa.Integer(), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('templates', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_clean_rooms')
