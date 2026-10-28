"""AI steward proposals awaiting review

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-28 19:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_steward_proposals',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('field', sa.String(length=512), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('priority', sa.Float(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('decided_by', sa.String(length=512), nullable=True),
    sa.Column('decided_ms', sa.BigInteger(), nullable=True),
    sa.Column('batch_id', sa.String(length=40), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cairn_steward_proposals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_steward_proposals_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_cairn_steward_proposals_urn'), ['urn'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_steward_proposals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_steward_proposals_urn'))
        batch_op.drop_index(batch_op.f('ix_cairn_steward_proposals_state'))
    op.drop_table('cairn_steward_proposals')
