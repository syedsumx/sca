"""Trino tables synced into the catalog

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-01 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_trino_catalog_tables',
    sa.Column('connection', sa.String(length=128), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.PrimaryKeyConstraint('connection', 'urn')
    )
    op.create_table('cairn_trino_catalog_runs',
    sa.Column('connection', sa.String(length=128), nullable=False),
    sa.Column('next_ms', sa.BigInteger(), nullable=False),
    sa.Column('last_ms', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('connection')
    )


def downgrade() -> None:
    op.drop_table('cairn_trino_catalog_runs')
    op.drop_table('cairn_trino_catalog_tables')
