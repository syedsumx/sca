"""Access reviews: campaigns, and an item per grant reviewed with its decision

Revision ID: 0102
Revises: 0101
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0102'
down_revision = '0101'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_access_reviews',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('due_ms', sa.BigInteger(), nullable=False),
        sa.Column('closed_ms', sa.BigInteger(), nullable=True),
        sa.Column('reminded_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_access_reviews_state', 'cairn_access_reviews', ['state'])
    op.create_index('ix_cairn_access_reviews_due_ms', 'cairn_access_reviews', ['due_ms'])
    op.create_table(
        'cairn_access_review_items',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('review_id', sa.String(length=40), nullable=False),
        sa.Column('grant_id', sa.String(length=64), nullable=False),
        sa.Column('persona_id', sa.String(length=64), nullable=False),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('source', sa.String(length=128), nullable=False),
        sa.Column('reviewers', sa.Text(), nullable=False),
        sa.Column('decision', sa.String(length=16), nullable=True),
        sa.Column('decided_by', sa.String(length=512), nullable=True),
        sa.Column('decided_ms', sa.BigInteger(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_access_review_items_review_id', 'cairn_access_review_items', ['review_id'])


def downgrade() -> None:
    op.drop_table('cairn_access_review_items')
    op.drop_table('cairn_access_reviews')
