"""Licence key and seats

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_licence',
    sa.Column('slot', sa.Integer(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('installed_by', sa.String(length=512), nullable=False),
    sa.Column('installed_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('slot')
    )
    op.create_table('cairn_licence_seats',
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('first_seen_ms', sa.BigInteger(), nullable=False),
    sa.Column('last_seen_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('actor')
    )
    op.create_index(op.f('ix_cairn_licence_seats_last_seen_ms'), 'cairn_licence_seats', ['last_seen_ms'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cairn_licence_seats_last_seen_ms'), table_name='cairn_licence_seats')
    op.drop_table('cairn_licence_seats')
    op.drop_table('cairn_licence')
