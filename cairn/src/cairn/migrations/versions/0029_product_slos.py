"""Data product SLOs and their last known state

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-01 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_product_slos',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('product_urn', sa.String(length=512), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cairn_product_slos_product_urn', 'cairn_product_slos', ['product_urn'], unique=False)
    op.create_table('cairn_slo_state',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('since_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('cairn_slo_state')
    op.drop_index('ix_cairn_product_slos_product_urn', table_name='cairn_product_slos')
    op.drop_table('cairn_product_slos')
