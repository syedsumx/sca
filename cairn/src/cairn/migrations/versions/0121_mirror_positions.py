"""Mirror positions long enough for MongoDB resume tokens

Revision ID: 0121
Revises: 0120
Create Date: 2026-11-07 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0121'
down_revision = '0120'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_mirrors') as b:
        b.alter_column('last_lsn', existing_type=sa.String(length=32), type_=sa.String(length=1024),
                       existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('cairn_mirrors') as b:
        b.alter_column('last_lsn', existing_type=sa.String(length=1024), type_=sa.String(length=32),
                       existing_nullable=True)
