"""Data residency follows copies made by SQL: what each Trino statement read from pinned namespaces and writes

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-09 22:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0136'
down_revision = '0135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_residency_flows',
        sa.Column('query_id', sa.String(length=128), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('namespace', sa.String(length=512), nullable=False),
        sa.Column('region', sa.String(length=32), nullable=True),
        sa.Column('allow', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('query_id', 'side', 'namespace'),
    )
    op.create_index('ix_cairn_residency_flows_created_ms', 'cairn_residency_flows', ['created_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_residency_flows_created_ms', table_name='cairn_residency_flows')
    op.drop_table('cairn_residency_flows')
