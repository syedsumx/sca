"""Bundles: what each bundle of declared resources manages

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_bundles',
        sa.Column('name', sa.String(length=128), primary_key=True),
        sa.Column('resources', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('applied_by', sa.String(length=256), nullable=False),
        sa.Column('applied_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_bundles')
