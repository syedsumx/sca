"""Context layer: query patterns, approved definitions and their versions

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-28 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_query_patterns',
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('sql', sa.Text(), nullable=False),
    sa.Column('tables', sa.Text(), nullable=False),
    sa.Column('vector', sa.Text(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('urn')
    )
    op.create_table('cairn_context_definitions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('finding_id', sa.String(length=40), nullable=True),
    sa.Column('approved_by', sa.String(length=512), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cairn_context_definition_versions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('approved_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id', 'version')
    )


def downgrade() -> None:
    op.drop_table('cairn_context_definition_versions')
    op.drop_table('cairn_context_definitions')
    op.drop_table('cairn_query_patterns')
