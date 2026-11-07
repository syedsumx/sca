"""The assistant's workspace: per-person memory notes and settings, saved artifacts, deep research runs

Revision ID: 0111
Revises: 0110
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0111'
down_revision = '0110'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_assistant_memory',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('conversation_id', sa.String(length=40), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_assistant_memory_actor', 'cairn_assistant_memory', ['actor'])
    op.create_table(
        'cairn_assistant_prefs',
        sa.Column('actor', sa.String(length=512), primary_key=True),
        sa.Column('memory_enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_assistant_artifacts',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('sources', sa.Text(), nullable=False),
        sa.Column('shared_with', sa.Text(), nullable=False),
        sa.Column('conversation_id', sa.String(length=40), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('history', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_assistant_artifacts_owner', 'cairn_assistant_artifacts', ['owner'])
    op.create_table(
        'cairn_research_runs',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('plan', sa.Text(), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('artifact_id', sa.String(length=40), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cancel', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_research_runs_actor', 'cairn_research_runs', ['actor'])


def downgrade() -> None:
    op.drop_index('ix_cairn_research_runs_actor', table_name='cairn_research_runs')
    op.drop_table('cairn_research_runs')
    op.drop_index('ix_cairn_assistant_artifacts_owner', table_name='cairn_assistant_artifacts')
    op.drop_table('cairn_assistant_artifacts')
    op.drop_table('cairn_assistant_prefs')
    op.drop_index('ix_cairn_assistant_memory_actor', table_name='cairn_assistant_memory')
    op.drop_table('cairn_assistant_memory')
