"""Privacy operations: subject requests, retention policies and their state

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-31 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_privacy_requests',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('identifier_kind', sa.String(length=32), nullable=False),
    sa.Column('identifier_hash', sa.String(length=64), nullable=False),
    sa.Column('identifier_hint', sa.String(length=200), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('due_ms', sa.BigInteger(), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_privacy_requests_identifier_hash', 'cairn_privacy_requests', ['identifier_hash'], unique=False)
    op.create_index('ix_cairn_privacy_requests_state', 'cairn_privacy_requests', ['state'], unique=False)
    op.create_table('cairn_retention_policies',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cairn_retention_state',
    sa.Column('dataset', sa.String(length=700), nullable=False),
    sa.Column('policy', sa.String(length=40), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('oldest_ms', sa.BigInteger(), nullable=True),
    sa.Column('detail', sa.String(length=500), nullable=False),
    sa.Column('checked_ms', sa.BigInteger(), nullable=False),
    sa.Column('notified', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('dataset', 'policy')
    )


def downgrade() -> None:
    op.drop_table('cairn_retention_state')
    op.drop_table('cairn_retention_policies')
    op.drop_index('ix_cairn_privacy_requests_state', table_name='cairn_privacy_requests')
    op.drop_index('ix_cairn_privacy_requests_identifier_hash', table_name='cairn_privacy_requests')
    op.drop_table('cairn_privacy_requests')
