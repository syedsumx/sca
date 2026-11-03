"""Materialized views: derived tables of kind materialized_view

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0064'
down_revision = '0063'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.add_column(sa.Column('kind', sa.String(length=32), nullable=False, server_default='table'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.drop_column('kind')
