"""Metrics published as SQL views for outside BI tools

Revision ID: 0091
Revises: 0090
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0091'
down_revision = '0090'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_metrics') as b:
        b.add_column(sa.Column('published', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('cairn_metrics') as b:
        b.drop_column('published')
