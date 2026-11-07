"""Point-lookup acceleration: maintenance policies' Bloom filter columns

Revision ID: 0108
Revises: 0107
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0108'
down_revision = '0107'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_maintenance_policies') as b:
        b.add_column(sa.Column('lookup', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_maintenance_policies') as b:
        b.drop_column('lookup')
