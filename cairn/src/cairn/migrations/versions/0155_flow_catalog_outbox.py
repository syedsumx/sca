"""Flows in the catalog: what Cairn Core has not taken yet waits here and is retried

Revision ID: 0155
Revises: 0154
Create Date: 2026-11-14 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0155'
down_revision = '0154'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_flow_catalog_outbox',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_flow_catalog_outbox_next_ms', 'cairn_flow_catalog_outbox', ['next_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_flow_catalog_outbox_next_ms', table_name='cairn_flow_catalog_outbox')
    op.drop_table('cairn_flow_catalog_outbox')
