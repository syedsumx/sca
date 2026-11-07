"""Semantic model autopilot: drafts built from tables and their use, and synonyms on metrics

Revision ID: 0118
Revises: 0117
Create Date: 2026-11-07 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0118'
down_revision = '0117'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_semantic_drafts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('refused', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('accepted', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_semantic_drafts_owner', 'cairn_semantic_drafts', ['owner'])
    with op.batch_alter_table('cairn_metrics') as b:
        b.add_column(sa.Column('synonyms', sa.Text(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_metrics') as b:
        b.drop_column('synonyms')
    op.drop_index('ix_cairn_semantic_drafts_owner', table_name='cairn_semantic_drafts')
    op.drop_table('cairn_semantic_drafts')
