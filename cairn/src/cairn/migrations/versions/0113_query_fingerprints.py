"""Adaptive warehouses: each query's fingerprint (its shape, literals left out) in query history

Revision ID: 0113
Revises: 0112
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0113'
down_revision = '0112'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_query_history') as b:
        b.add_column(sa.Column('fingerprint', sa.String(length=32), nullable=True))
    op.create_index('ix_cairn_query_history_fingerprint', 'cairn_query_history', ['fingerprint'])


def downgrade() -> None:
    op.drop_index('ix_cairn_query_history_fingerprint', table_name='cairn_query_history')
    with op.batch_alter_table('cairn_query_history') as b:
        b.drop_column('fingerprint')
