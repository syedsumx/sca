"""Pipes: files landing in a store prefix loaded into a lakehouse table, once each

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_pipes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=256), nullable=False, unique=True),
        sa.Column('bucket', sa.String(length=256), nullable=False),
        sa.Column('prefix', sa.String(length=1024), nullable=False),
        sa.Column('format', sa.String(length=16), nullable=False),
        sa.Column('target', sa.String(length=512), nullable=False),
        sa.Column('create_table', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_pipe_files',
        sa.Column('pipe_id', sa.String(length=32), primary_key=True),
        sa.Column('key', sa.String(length=1024), primary_key=True),
        sa.Column('version', sa.String(length=64), primary_key=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('rows', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('loaded_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_pipe_files')
    op.drop_table('cairn_pipes')
