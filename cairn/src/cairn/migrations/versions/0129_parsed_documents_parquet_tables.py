"""Parsed documents (tables and charts from PDFs and images)

Revision ID: 0129
Revises: 0127
Create Date: 2026-11-08 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0129'
down_revision = '0127'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_parsed_documents',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('tables', sa.Integer(), nullable=False),
        sa.Column('charts', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('errors', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_parsed_documents_created_by', 'cairn_parsed_documents', ['created_by'])


def downgrade() -> None:
    op.drop_index('ix_cairn_parsed_documents_created_by', table_name='cairn_parsed_documents')
    op.drop_table('cairn_parsed_documents')
