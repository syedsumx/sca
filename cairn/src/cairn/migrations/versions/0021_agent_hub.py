"""Agent hub: the pause switch and per-agent daily usage

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-30 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_agent_settings',
    sa.Column('key', sa.String(length=32), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('cairn_agent_usage',
    sa.Column('agent', sa.String(length=40), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('agent', 'day')
    )


def downgrade() -> None:
    op.drop_table('cairn_agent_usage')
    op.drop_table('cairn_agent_settings')
