"""Source management: owners and last known health of each ingestion source

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-31 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_source_meta',
    sa.Column('urn', sa.String(length=300), nullable=False),
    sa.Column('owners', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('health', sa.String(length=16), nullable=False),
    sa.Column('health_since_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('urn')
    )


def downgrade() -> None:
    op.drop_table('cairn_source_meta')
