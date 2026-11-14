"""Workbooks: saved spreadsheet-style explorations

Revision ID: 0073
Revises: 0072
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0073'
down_revision = '0072'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_workbooks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_workbooks_owner', 'cairn_workbooks', ['owner'])


def downgrade() -> None:
    op.drop_table('cairn_workbooks')
