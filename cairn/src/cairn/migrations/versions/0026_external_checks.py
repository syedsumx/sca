"""Results from other data-quality tools (dbt tests, Great Expectations, Soda, generic)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-31 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_external_checks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('tool', sa.String(length=32), nullable=False),
    sa.Column('dataset', sa.String(length=700), nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('column_name', sa.String(length=300), nullable=True),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('url', sa.String(length=1000), nullable=True),
    sa.Column('last_ms', sa.BigInteger(), nullable=False),
    sa.Column('history', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_external_checks_dataset', 'cairn_external_checks', ['dataset'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_external_checks_dataset', table_name='cairn_external_checks')
    op.drop_table('cairn_external_checks')
