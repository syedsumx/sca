"""Differentially private clean rooms: the room's privacy settings and the epsilon it has spent

Revision ID: 0100
Revises: 0099
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0100'
down_revision = '0099'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_clean_rooms') as t:
        t.add_column(sa.Column('privacy', sa.Text(), nullable=True))
        t.add_column(sa.Column('epsilon_spent', sa.Float(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_clean_rooms') as t:
        t.drop_column('epsilon_spent')
        t.drop_column('privacy')
