"""Installed extensions

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-29 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_extensions',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('manifest', sa.Text(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('signing_secret', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('installed_by', sa.String(length=512), nullable=False),
    sa.Column('installed_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('name'),
    sa.UniqueConstraint('token_hash')
    )


def downgrade() -> None:
    op.drop_table('cairn_extensions')
