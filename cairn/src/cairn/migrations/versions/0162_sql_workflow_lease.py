"""SQL workflows: a lease while a run is under way, so one workflow never runs twice at once

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-17 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0162'
down_revision = '0161'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_sql_workflows') as b:
        b.add_column(sa.Column('running_until_ms', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_sql_workflows') as b:
        b.drop_column('running_until_ms')
