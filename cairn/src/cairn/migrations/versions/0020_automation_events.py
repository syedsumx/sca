"""Event triggers for automations: the durable event queue

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-30 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_automation_events',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('type', sa.String(length=48), nullable=False),
    sa.Column('urn', sa.String(length=1024), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('processed_ms', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_automation_events_state', 'cairn_automation_events', ['state'], unique=False)
    op.create_index('ix_cairn_automation_events_created_ms', 'cairn_automation_events', ['created_ms'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_automation_events_created_ms', table_name='cairn_automation_events')
    op.drop_index('ix_cairn_automation_events_state', table_name='cairn_automation_events')
    op.drop_table('cairn_automation_events')
