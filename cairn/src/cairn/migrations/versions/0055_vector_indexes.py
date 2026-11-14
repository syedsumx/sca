"""Vector indexes on lakehouse tables (search by meaning over a table's text)

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0055'
down_revision = '0054'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_vector_indexes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('source', sa.String(length=512), nullable=False),
        sa.Column('key_column', sa.String(length=128), nullable=False),
        sa.Column('text_column', sa.String(length=128), nullable=False),
        sa.Column('chunk_chars', sa.Integer(), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('refreshed_ms', sa.BigInteger(), nullable=True),
        sa.Column('source_snapshot', sa.BigInteger(), nullable=True),
        sa.Column('chunks', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_vector_indexes')
