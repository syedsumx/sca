"""Data products published to Snowflake listings and Delta Sharing shares

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-29 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_product_publications',
    sa.Column('product_urn', sa.String(length=512), nullable=False),
    sa.Column('target', sa.String(length=16), nullable=False),
    sa.Column('options', sa.Text(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('tables', sa.Text(), nullable=False),
    sa.Column('published_by', sa.String(length=512), nullable=False),
    sa.Column('published_ms', sa.BigInteger(), nullable=False),
    sa.Column('notified_fingerprint', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('product_urn', 'target')
    )


def downgrade() -> None:
    op.drop_table('cairn_product_publications')
