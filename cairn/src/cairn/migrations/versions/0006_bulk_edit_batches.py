"""Bulk metadata edit batches, kept so an import can be undone

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28 06:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_bulk_batches',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('changes', sa.Text(), nullable=False),
    sa.Column('before', sa.Text(), nullable=True),
    sa.Column('after', sa.Text(), nullable=True),
    sa.Column('rows', sa.Integer(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cairn_bulk_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_bulk_batches_actor'), ['actor'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_bulk_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_bulk_batches_actor'))
    op.drop_table('cairn_bulk_batches')
