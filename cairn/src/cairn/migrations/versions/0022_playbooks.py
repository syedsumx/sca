"""Agent playbooks: which are on, and their runs with steps

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-30 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_playbooks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('config', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=512), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cairn_playbook_runs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('playbook', sa.String(length=40), nullable=False),
    sa.Column('subject', sa.String(length=1024), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('steps', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_playbook_runs_playbook', 'cairn_playbook_runs', ['playbook'], unique=False)
    op.create_index('ix_cairn_playbook_runs_created_ms', 'cairn_playbook_runs', ['created_ms'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_playbook_runs_created_ms', table_name='cairn_playbook_runs')
    op.drop_index('ix_cairn_playbook_runs_playbook', table_name='cairn_playbook_runs')
    op.drop_table('cairn_playbook_runs')
    op.drop_table('cairn_playbooks')
