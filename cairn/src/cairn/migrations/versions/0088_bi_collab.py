"""BI folders, comments and usage

Revision ID: 0088
Revises: 0087
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0088'
down_revision = '0087'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bi_folders',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('parent_id', sa.String(length=32), nullable=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_bi_folders_parent_id', 'cairn_bi_folders', ['parent_id'])
    op.create_index('ix_cairn_bi_folders_owner', 'cairn_bi_folders', ['owner'])
    op.create_table(
        'cairn_bi_folder_items',
        sa.Column('folder_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('folder_id', 'kind', 'asset_id'),
    )
    op.create_table(
        'cairn_bi_comments',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.Column('tile', sa.Integer(), nullable=True),
        sa.Column('parent_id', sa.String(length=32), nullable=True),
        sa.Column('author', sa.String(length=256), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('deleted', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('edited_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_bi_comments_asset', 'cairn_bi_comments', ['kind', 'asset_id'])
    op.create_table(
        'cairn_bi_views',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('asset_id', sa.String(length=64), nullable=False),
        sa.Column('viewer', sa.String(length=256), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'asset_id', 'viewer', 'day'),
    )
    op.create_index('ix_cairn_bi_views_day', 'cairn_bi_views', ['day'])


def downgrade() -> None:
    op.drop_index('ix_cairn_bi_views_day', table_name='cairn_bi_views')
    op.drop_table('cairn_bi_views')
    op.drop_index('ix_cairn_bi_comments_asset', table_name='cairn_bi_comments')
    op.drop_table('cairn_bi_comments')
    op.drop_table('cairn_bi_folder_items')
    op.drop_index('ix_cairn_bi_folders_owner', table_name='cairn_bi_folders')
    op.drop_index('ix_cairn_bi_folders_parent_id', table_name='cairn_bi_folders')
    op.drop_table('cairn_bi_folders')
