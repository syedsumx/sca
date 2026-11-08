"""Profiles (role, team, persona, language, pins, the assistant's standing instructions) and web push subscriptions

Revision ID: 0131
Revises: 0127
Create Date: 2026-11-08 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0131'
down_revision = '0127'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_profiles',
        sa.Column('actor', sa.String(length=512), primary_key=True),
        sa.Column('role', sa.String(length=120), nullable=True),
        sa.Column('team', sa.String(length=120), nullable=True),
        sa.Column('default_persona', sa.String(length=120), nullable=True),
        sa.Column('language', sa.String(length=8), nullable=True),
        sa.Column('pinned', sa.Text(), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_push_subscriptions',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('endpoint_hash', sa.String(length=64), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_sent_ms', sa.BigInteger(), nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.UniqueConstraint('endpoint_hash'),
    )
    op.create_index('ix_cairn_push_subscriptions_actor', 'cairn_push_subscriptions', ['actor'])


def downgrade() -> None:
    op.drop_index('ix_cairn_push_subscriptions_actor', table_name='cairn_push_subscriptions')
    op.drop_table('cairn_push_subscriptions')
    op.drop_table('cairn_profiles')
