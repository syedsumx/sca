"""Automatic clustering: maintenance policies' cluster settings, and each table's clustering

Revision ID: 0067
Revises: 0066
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0067'
down_revision = '0066'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_maintenance_policies') as b:
        b.add_column(sa.Column('cluster', sa.String(length=512), nullable=True))
        b.add_column(sa.Column('cluster_depth', sa.Float(), nullable=False, server_default='4.0'))
    op.create_table(
        'cairn_table_clustering',
        sa.Column('table_name', sa.String(length=512), primary_key=True),
        sa.Column('columns', sa.String(length=512), nullable=False),
        sa.Column('chosen_by', sa.String(length=256), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('depth', sa.Float(), nullable=True),
        sa.Column('files', sa.Integer(), nullable=True),
        sa.Column('checked_ms', sa.BigInteger(), nullable=False),
        sa.Column('rewritten_ms', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_table_clustering')
    with op.batch_alter_table('cairn_maintenance_policies') as b:
        b.drop_column('cluster_depth')
        b.drop_column('cluster')
