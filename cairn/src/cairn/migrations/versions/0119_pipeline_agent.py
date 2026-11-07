"""Pipeline agent: tasks that write, try and ship pipelines as the person asking

Revision ID: 0119
Revises: 0118
Create Date: 2026-11-07 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0119'
down_revision = '0118'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_pipeline_agent_tasks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('target', sa.String(length=512), nullable=False),
        sa.Column('derived_id', sa.String(length=32), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('proposal', sa.Text(), nullable=True),
        sa.Column('checks', sa.Text(), nullable=False),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('outcome', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_pipeline_agent_tasks_owner', 'cairn_pipeline_agent_tasks', ['owner'])


def downgrade() -> None:
    op.drop_index('ix_cairn_pipeline_agent_tasks_owner', table_name='cairn_pipeline_agent_tasks')
    op.drop_table('cairn_pipeline_agent_tasks')
