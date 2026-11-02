"""Streams: Kafka topics (CDC included) written into lakehouse tables

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_streams',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=256), nullable=False, unique=True),
        sa.Column('topic', sa.String(length=249), nullable=False),
        sa.Column('format', sa.String(length=16), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('key', sa.String(length=1024), nullable=False),
        sa.Column('target', sa.String(length=512), nullable=False),
        sa.Column('create_table', sa.Boolean(), nullable=False),
        sa.Column('evolve', sa.Boolean(), nullable=False),
        sa.Column('start', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=1024), nullable=True),
        sa.Column('batch_rows', sa.Integer(), nullable=False),
        sa.Column('batch_seconds', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_stream_status',
        sa.Column('stream_id', sa.String(length=32), primary_key=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('rows', sa.BigInteger(), nullable=False),
        sa.Column('lag', sa.BigInteger(), nullable=True),
        sa.Column('last_commit_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_stream_rejects',
        sa.Column('stream_id', sa.String(length=32), primary_key=True),
        sa.Column('partition', sa.Integer(), primary_key=True),
        sa.Column('offset', sa.BigInteger(), primary_key=True),
        sa.Column('reason', sa.String(length=1000), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('rejected_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_stream_rejects_rejected_ms', 'cairn_stream_rejects', ['rejected_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_stream_rejects_rejected_ms', table_name='cairn_stream_rejects')
    op.drop_table('cairn_stream_rejects')
    op.drop_table('cairn_stream_status')
    op.drop_table('cairn_streams')
