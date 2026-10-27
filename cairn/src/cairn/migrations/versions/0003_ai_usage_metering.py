"""AI usage metering: one row per model call with tokens and estimated cost

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_ai_usage',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('ts_ms', sa.BigInteger(), nullable=False),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('task', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cairn_ai_usage', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_ai_usage_actor'), ['actor'], unique=False)
        batch_op.create_index(batch_op.f('ix_cairn_ai_usage_ts_ms'), ['ts_ms'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_ai_usage', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_ai_usage_ts_ms'))
        batch_op.drop_index(batch_op.f('ix_cairn_ai_usage_actor'))

    op.drop_table('cairn_ai_usage')
