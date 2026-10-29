"""Knowledge sources: Confluence, Notion, Google Docs and SharePoint pages linked to assets

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-31 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_knowledge_pages',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.Column('linked', sa.Text(), nullable=False),
    sa.Column('vector', sa.Text(), nullable=False),
    sa.Column('synced_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_knowledge_pages_source', 'cairn_knowledge_pages', ['source'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_knowledge_pages_source', table_name='cairn_knowledge_pages')
    op.drop_table('cairn_knowledge_pages')
