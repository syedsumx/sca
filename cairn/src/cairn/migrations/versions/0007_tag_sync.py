"""Warehouse tag sync: the tags Cairn applied per target, and sync runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_tag_sync_state',
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('tag', sa.String(length=256), nullable=False),
    sa.Column('value', sa.String(length=256), nullable=False),
    sa.Column('synced_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('target_id', 'object_key', 'tag')
    )
    op.create_table('cairn_tag_sync_runs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('dry_run', sa.String(length=5), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('started_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cairn_tag_sync_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_tag_sync_runs_target_id'), ['target_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_tag_sync_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_tag_sync_runs_target_id'))
    op.drop_table('cairn_tag_sync_runs')
    op.drop_table('cairn_tag_sync_state')
