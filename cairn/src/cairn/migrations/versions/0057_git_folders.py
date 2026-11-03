"""Git folders: saved queries and notebooks kept in a git repository

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0057'
down_revision = '0056'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_git_folders',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=64), nullable=True),
        sa.Column('url', sa.String(length=1024), nullable=True),
        sa.Column('branch', sa.String(length=256), nullable=False),
        sa.Column('path', sa.String(length=512), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=True),
        sa.Column('head', sa.String(length=64), nullable=True),
        sa.Column('synced_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_git_folders_owner', 'cairn_git_folders', ['owner'])
    op.create_table(
        'cairn_git_folder_files',
        sa.Column('folder_id', sa.String(length=32), primary_key=True),
        sa.Column('file', sa.String(length=1024), primary_key=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('ref', sa.String(length=512), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('pushed', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_git_folder_files')
    op.drop_table('cairn_git_folders')
