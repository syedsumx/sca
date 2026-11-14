"""Stored procedures: named, parameterized SQL scripts run as their caller

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0058'
down_revision = '0057'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_procedures',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('description', sa.String(length=2000), nullable=False),
        sa.Column('params', sa.Text(), nullable=False),
        sa.Column('statements', sa.Text(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_procedures_owner', 'cairn_procedures', ['owner'])


def downgrade() -> None:
    op.drop_table('cairn_procedures')
