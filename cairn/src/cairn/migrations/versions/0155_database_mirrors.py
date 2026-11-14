"""Database mirrors (L166): a whole PostgreSQL schema mirrored, and each of its tables' state

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-14 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0155'
down_revision = '0154'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_database_mirrors',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('schema', sa.String(length=128), nullable=False),
        sa.Column('target_namespace', sa.String(length=128), nullable=False),
        sa.Column('include', sa.Text(), nullable=False),
        sa.Column('exclude', sa.Text(), nullable=False),
        sa.Column('defaults', sa.Text(), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('last_lsn', sa.String(length=64), nullable=True),
        sa.Column('last_commit_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_database_mirror_tables',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('mirror_id', sa.String(length=32), nullable=False),
        sa.Column('source_oid', sa.BigInteger(), nullable=False),
        sa.Column('source_name', sa.String(length=128), nullable=False),
        sa.Column('target', sa.String(length=256), nullable=False),
        sa.Column('key', sa.String(length=1024), nullable=False),
        sa.Column('columns', sa.Text(), nullable=False),
        sa.Column('renames', sa.Text(), nullable=False),
        sa.Column('gone', sa.Text(), nullable=False),
        sa.Column('applied', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('rows', sa.BigInteger(), nullable=False),
        sa.Column('snapshot_done', sa.Boolean(), nullable=False),
        sa.Column('copy_lsn', sa.String(length=64), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('mirror_id', 'source_oid', name='uq_cairn_database_mirror_tables_oid'),
    )
    op.create_index('ix_cairn_database_mirror_tables_mirror_id', 'cairn_database_mirror_tables', ['mirror_id'])


def downgrade() -> None:
    op.drop_index('ix_cairn_database_mirror_tables_mirror_id', table_name='cairn_database_mirror_tables')
    op.drop_table('cairn_database_mirror_tables')
    op.drop_table('cairn_database_mirrors')
