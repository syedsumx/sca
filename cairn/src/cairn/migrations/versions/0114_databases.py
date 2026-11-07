"""Operational databases: PostgreSQL databases made through Cairn, and the short-lived logins handed out for them

Revision ID: 0114
Revises: 0113
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0114'
down_revision = '0113'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_databases',
        sa.Column('name', sa.String(length=31), primary_key=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('branch_of', sa.String(length=31), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_database_logins',
        sa.Column('role', sa.String(length=63), primary_key=True),
        sa.Column('database', sa.String(length=31), nullable=False),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('access', sa.String(length=8), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
        sa.Column('dropped', sa.Boolean(), nullable=False),
    )
    op.create_index('ix_cairn_database_logins_database', 'cairn_database_logins', ['database'])
    op.create_index('ix_cairn_database_logins_actor', 'cairn_database_logins', ['actor'])
    op.create_index('ix_cairn_database_logins_expires_ms', 'cairn_database_logins', ['expires_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_database_logins_expires_ms', table_name='cairn_database_logins')
    op.drop_index('ix_cairn_database_logins_actor', table_name='cairn_database_logins')
    op.drop_index('ix_cairn_database_logins_database', table_name='cairn_database_logins')
    op.drop_table('cairn_database_logins')
    op.drop_table('cairn_databases')
