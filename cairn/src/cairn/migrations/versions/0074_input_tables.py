"""Input tables: lakehouse tables people type into

Revision ID: 0074
Revises: 0073
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0074'
down_revision = '0073'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_input_tables',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('table_name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('columns', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_input_tables')
