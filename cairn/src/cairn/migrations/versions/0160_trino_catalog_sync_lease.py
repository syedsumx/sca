"""Trino catalog sync: a lease while a connection's sync runs, so one connection is never synced twice at once

Revision ID: 0160
Revises: 0159
Create Date: 2026-11-18 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0160'
down_revision = '0159'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_trino_catalog_runs') as b:
        b.add_column(sa.Column('lease_until', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_trino_catalog_runs') as b:
        b.drop_column('lease_until')
