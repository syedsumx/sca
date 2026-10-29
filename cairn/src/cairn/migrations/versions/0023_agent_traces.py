"""Agent observability: tool-call traces and feedback on AI answers

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-30 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_agent_calls',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('agent', sa.String(length=40), nullable=True),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('channel', sa.String(length=12), nullable=False),
    sa.Column('tool', sa.String(length=64), nullable=False),
    sa.Column('args', sa.String(length=600), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('flagged', sa.Integer(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_agent_calls_agent', 'cairn_agent_calls', ['agent'], unique=False)
    op.create_index('ix_cairn_agent_calls_actor', 'cairn_agent_calls', ['actor'], unique=False)
    op.create_index('ix_cairn_agent_calls_created_ms', 'cairn_agent_calls', ['created_ms'], unique=False)
    op.create_table('cairn_ai_feedback',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('rating', sa.String(length=4), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('comment', sa.String(length=2000), nullable=False),
    sa.Column('context', sa.Text(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_ai_feedback_created_ms', 'cairn_ai_feedback', ['created_ms'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_ai_feedback_created_ms', table_name='cairn_ai_feedback')
    op.drop_table('cairn_ai_feedback')
    op.drop_index('ix_cairn_agent_calls_created_ms', table_name='cairn_agent_calls')
    op.drop_index('ix_cairn_agent_calls_actor', table_name='cairn_agent_calls')
    op.drop_index('ix_cairn_agent_calls_agent', table_name='cairn_agent_calls')
    op.drop_table('cairn_agent_calls')
