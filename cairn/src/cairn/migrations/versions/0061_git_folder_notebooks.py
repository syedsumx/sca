"""Git folders: notebooks saved in Jupyter under a folder, waiting to be pushed

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0061'
down_revision = '0060'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_git_folder_notebooks',
        sa.Column('folder_id', sa.String(length=32), primary_key=True),
        sa.Column('file', sa.String(length=1024), primary_key=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('saved_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_git_folder_notebooks')
