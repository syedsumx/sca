"""AI system inventory

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_ai_systems',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('owner', sa.String(length=512), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=True),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('decided_by', sa.String(length=512), nullable=True),
    sa.Column('decided_ms', sa.BigInteger(), nullable=True),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cairn_ai_systems_owner'), 'cairn_ai_systems', ['owner'], unique=False)
    op.create_index(op.f('ix_cairn_ai_systems_state'), 'cairn_ai_systems', ['state'], unique=False)
    op.create_index(op.f('ix_cairn_ai_systems_tier'), 'cairn_ai_systems', ['tier'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cairn_ai_systems_tier'), table_name='cairn_ai_systems')
    op.drop_index(op.f('ix_cairn_ai_systems_state'), table_name='cairn_ai_systems')
    op.drop_index(op.f('ix_cairn_ai_systems_owner'), table_name='cairn_ai_systems')
    op.drop_table('cairn_ai_systems')
