"""Input-table approvals: reviewers per table and change requests

Revision ID: 0087
Revises: 0086
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0087'
down_revision = '0086'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_input_approval',
        sa.Column('table_id', sa.String(length=32), primary_key=True),
        sa.Column('reviewers', sa.Text(), nullable=False),
        sa.Column('set_by', sa.String(length=256), nullable=False),
        sa.Column('set_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_input_changes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('table_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('author', sa.String(length=256), nullable=False),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('reviewer', sa.String(length=256), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('decided_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_input_changes_table_id', 'cairn_input_changes', ['table_id'])
    op.create_index('ix_cairn_input_changes_state', 'cairn_input_changes', ['state'])


def downgrade() -> None:
    op.drop_index('ix_cairn_input_changes_state', table_name='cairn_input_changes')
    op.drop_index('ix_cairn_input_changes_table_id', table_name='cairn_input_changes')
    op.drop_table('cairn_input_changes')
    op.drop_table('cairn_input_approval')
