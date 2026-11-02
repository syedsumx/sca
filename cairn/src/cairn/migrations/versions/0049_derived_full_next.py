"""Derived tables: rebuild from the whole query after the definition changes

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0049'
down_revision = '0048'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.add_column(sa.Column('full_next', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.drop_column('full_next')
