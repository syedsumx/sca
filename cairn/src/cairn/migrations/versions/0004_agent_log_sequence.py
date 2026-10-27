"""Remote agents: log upload sequence, so retried log uploads are stored once

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_agent_tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_log_seq', sa.Integer(), server_default='-1', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('cairn_agent_tasks', schema=None) as batch_op:
        batch_op.drop_column('last_log_seq')
