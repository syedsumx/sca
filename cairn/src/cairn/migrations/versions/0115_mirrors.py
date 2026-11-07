"""Mirrors: PostgreSQL tables kept in lakehouse tables through logical replication

Revision ID: 0115
Revises: 0114
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0115'
down_revision = '0114'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_mirrors',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('source', sa.String(length=256), nullable=False),
        sa.Column('target', sa.String(length=256), nullable=False),
        sa.Column('key', sa.String(length=1024), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('snapshot_done', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('rows', sa.BigInteger(), nullable=False),
        sa.Column('last_lsn', sa.String(length=32), nullable=True),
        sa.Column('last_commit_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_mirrors')
