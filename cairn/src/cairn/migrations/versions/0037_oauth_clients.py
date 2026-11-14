"""Cairn's OAuth2 sign-in serves several clients (Trino, Airflow): each code remembers its client and the
signed-in person's groups

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-02 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_trino_oauth_codes') as b:
        b.add_column(sa.Column('client_id', sa.String(length=64), nullable=False, server_default='trino'))
        b.add_column(sa.Column('groups', sa.Text(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_trino_oauth_codes') as b:
        b.drop_column('groups')
        b.drop_column('client_id')
