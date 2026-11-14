"""SaaS syncs (L165): a declarative REST source's spec

Revision ID: 0153
Revises: 0152
Create Date: 2026-11-14 00:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0153'
down_revision = '0152'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_saas_syncs') as b:
        b.add_column(sa.Column('spec', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_saas_syncs') as b:
        b.drop_column('spec')
