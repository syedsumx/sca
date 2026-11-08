"""Agent tool calls name the run they were part of (trace intelligence: loops within a run); custom agents declare
the intent their runs act under (intent-based security)

Revision ID: 0124
Revises: 0123
Create Date: 2026-11-08 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0124'
down_revision = '0123'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_agent_calls') as t:
        t.add_column(sa.Column('run', sa.String(length=40), nullable=True))
    op.create_index('ix_cairn_agent_calls_run', 'cairn_agent_calls', ['run'])
    with op.batch_alter_table('cairn_custom_agents') as t:
        t.add_column(sa.Column('intent', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_custom_agents') as t:
        t.drop_column('intent')
    op.drop_index('ix_cairn_agent_calls_run', table_name='cairn_agent_calls')
    with op.batch_alter_table('cairn_agent_calls') as t:
        t.drop_column('run')
