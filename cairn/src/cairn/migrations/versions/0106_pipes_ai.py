"""Files pipes' AI enrichment: images described and labelled, fields extracted from documents

Revision ID: 0106
Revises: 0105
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0106'
down_revision = '0105'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_pipes') as b:
        b.add_column(sa.Column('ai', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_pipes') as b:
        b.drop_column('ai')
