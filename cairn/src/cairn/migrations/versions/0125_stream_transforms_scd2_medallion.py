"""Streaming transformations on streams; SCD type 2 derived tables; medallion layers

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
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.add_column(sa.Column('tracked', sa.Text(), nullable=True))
    op.create_table(
        'cairn_medallion_layers',
        sa.Column('dataset_urn', sa.String(length=512), primary_key=True),
        sa.Column('table_name', sa.String(length=512), nullable=False),
        sa.Column('layer', sa.String(length=16), nullable=False),
        sa.Column('resource', sa.String(length=300), nullable=True),
        sa.Column('set_by', sa.String(length=256), nullable=False),
        sa.Column('set_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_medallion_layers')
    with op.batch_alter_table('cairn_derived_tables') as b:
        b.drop_column('tracked')
    with op.batch_alter_table('cairn_streams') as b:
        b.drop_column('transform')
