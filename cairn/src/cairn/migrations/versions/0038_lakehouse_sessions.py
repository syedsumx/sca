"""Lakehouse sessions: an engine's sign-in to Cairn Catalog, renewed with a session credential (hashed)
until it is ended or reaches its maximum length

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-02 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_lakehouse_sessions',
        sa.Column('sid', sa.String(length=64), primary_key=True),
        sa.Column('user', sa.String(length=256), nullable=False),
        sa.Column('secret_hash', sa.String(length=64), nullable=False),
        sa.Column('started_s', sa.BigInteger(), nullable=False),
        sa.Column('ended_s', sa.BigInteger(), nullable=True),
        sa.Column('ended_by', sa.String(length=256), nullable=True),
    )
    op.create_index('ix_cairn_lakehouse_sessions_user', 'cairn_lakehouse_sessions', ['user'])


def downgrade() -> None:
    op.drop_index('ix_cairn_lakehouse_sessions_user', table_name='cairn_lakehouse_sessions')
    op.drop_table('cairn_lakehouse_sessions')
