"""Serving tables: lakehouse tables kept as row stores in PostgreSQL for lookups by key

Revision ID: 0127
Revises: 0123
Create Date: 2026-11-08 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0127'
down_revision = '0123'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_serving_tables',
        sa.Column('name', sa.String(length=40), primary_key=True),
        sa.Column('source', sa.String(length=512), nullable=False),
        sa.Column('source_urn', sa.String(length=1024), nullable=False),
        sa.Column('primary_key', sa.Text(), nullable=False),
        sa.Column('indexes', sa.Text(), nullable=False),
        sa.Column('writable', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('pg_table', sa.String(length=63), nullable=True),
        sa.Column('types', sa.Text(), nullable=True),
        sa.Column('synced_ms', sa.BigInteger(), nullable=True),
        sa.Column('synced_snapshot', sa.BigInteger(), nullable=True),
        sa.Column('synced_snapshot_ms', sa.BigInteger(), nullable=True),
        sa.Column('source_snapshot', sa.BigInteger(), nullable=True),
        sa.Column('behind_since_ms', sa.BigInteger(), nullable=True),
        sa.Column('rows', sa.BigInteger(), nullable=True),
        sa.Column('last_mode', sa.String(length=16), nullable=True),
        sa.Column('last_written', sa.Integer(), nullable=True),
        sa.Column('last_removed', sa.Integer(), nullable=True),
        sa.Column('last_duration_ms', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_serving_tables')
