"""Data apps: code from git, and a Kubernetes runtime

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0062'
down_revision = '0061'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_data_apps') as b:
        b.add_column(sa.Column('runtime', sa.String(length=16), nullable=False, server_default='local'))
        b.add_column(sa.Column('git', sa.Text(), nullable=True))
        b.add_column(sa.Column('image', sa.String(length=512), nullable=True))
        b.add_column(sa.Column('port', sa.Integer(), nullable=True))
        b.add_column(sa.Column('commit', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('deployed_ms', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_data_apps') as b:
        for c in ('deployed_ms', 'commit', 'port', 'image', 'git', 'runtime'):
            b.drop_column(c)
