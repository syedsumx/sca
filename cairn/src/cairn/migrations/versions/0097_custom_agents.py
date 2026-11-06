"""Custom agents built and deployed in Cairn, and their runs

Revision ID: 0097
Revises: 0096
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0097'
down_revision = '0096'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_custom_agents',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('team', sa.String(length=256), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=False),
        sa.Column('tools', sa.Text(), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('ai_system', sa.String(length=40), nullable=True),
        sa.Column('max_steps', sa.Integer(), nullable=False),
        sa.Column('agent_id', sa.String(length=40), nullable=False),
        sa.Column('deployed', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('deployed_version', sa.Integer(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_custom_agents_owner', 'cairn_custom_agents', ['owner'])
    op.create_table(
        'cairn_custom_agent_runs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('agent', sa.String(length=32), nullable=False),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('input', sa.Text(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('calls', sa.Text(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_custom_agent_runs_agent', 'cairn_custom_agent_runs', ['agent'])
    op.create_index('ix_cairn_custom_agent_runs_actor', 'cairn_custom_agent_runs', ['actor'])
    op.create_index('ix_cairn_custom_agent_runs_started_ms', 'cairn_custom_agent_runs', ['started_ms'])


def downgrade() -> None:
    op.drop_table('cairn_custom_agent_runs')
    op.drop_table('cairn_custom_agents')
