"""The assistant's memory: the datasets a note was made from

Revision ID: 0161
Revises: 0160
Create Date: 2026-11-16 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0161'
down_revision = '0160'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_assistant_memory') as b:
        b.add_column(sa.Column('sources', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_assistant_memory') as b:
        b.drop_column('sources')
