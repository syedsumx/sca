"""Document sources: SharePoint and Google Drive folders kept in a store prefix for files pipes

Revision ID: 0122
Revises: 0121
Create Date: 2026-11-07 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0122'
down_revision = '0121'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_document_sources',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('location', sa.Text(), nullable=False),
        sa.Column('bucket', sa.String(length=256), nullable=False),
        sa.Column('prefix', sa.String(length=1024), nullable=False),
        sa.Column('secret_scope', sa.String(length=128), nullable=False),
        sa.Column('cron', sa.String(length=100), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('files', sa.Integer(), nullable=False),
        sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('next_run_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_document_source_items',
        sa.Column('source_id', sa.String(length=32), primary_key=True),
        sa.Column('item_id', sa.String(length=256), primary_key=True),
        sa.Column('key', sa.String(length=1024), nullable=False),
        sa.Column('version', sa.String(length=256), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('synced_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_document_source_items')
    op.drop_table('cairn_document_sources')
