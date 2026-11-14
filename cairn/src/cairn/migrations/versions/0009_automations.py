"""No-code automations and their runs

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-28 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_automations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('spec', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.Column('last_run_ms', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cairn_automation_runs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('automation_id', sa.String(length=40), nullable=False),
    sa.Column('started_ms', sa.BigInteger(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('matched', sa.Integer(), nullable=False),
    sa.Column('changed', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.String(length=40), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cairn_automation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_automation_runs_automation_id'), ['automation_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_automation_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_automation_runs_automation_id'))
    op.drop_table('cairn_automation_runs')
    op.drop_table('cairn_automations')
