"""Agent memory; AI gateway routes

Revision ID: 0128
Revises: 0127
Create Date: 2026-11-08 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0128'
down_revision = '0127'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_agent_memories',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('agent', sa.String(length=40), nullable=False),
        sa.Column('scope', sa.String(length=8), nullable=False),
        sa.Column('subject', sa.String(length=512), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('source_run', sa.String(length=64), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_used_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_agent_memories_agent_subject', 'cairn_agent_memories', ['agent', 'scope', 'subject'])
    op.create_index('ix_cairn_agent_memories_subject', 'cairn_agent_memories', ['subject'])
    op.create_table(
        'cairn_ai_gateway_routes',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('updated_by', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_ai_gateway_routes')
    op.drop_table('cairn_agent_memories')
