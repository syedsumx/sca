"""Streaming transformations on streams

Revision ID: 0125
Revises: 0123
Create Date: 2026-11-08 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0125'
down_revision = '0123'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_streams') as b:
        b.add_column(sa.Column('transform', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_streams') as b:
        b.drop_column('transform')
