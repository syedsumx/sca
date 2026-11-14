"""Data apps: web apps served by Cairn, each viewer signed in and governed as themselves

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0059'
down_revision = '0058'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_data_apps',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('url', sa.String(length=1024), nullable=True),
        sa.Column('command', sa.Text(), nullable=True),
        sa.Column('workdir', sa.String(length=1024), nullable=True),
        sa.Column('idle_minutes', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_data_apps')
