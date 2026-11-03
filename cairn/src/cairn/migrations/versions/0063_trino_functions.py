"""Stored functions Cairn published to its catalog

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0063'
down_revision = '0062'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_trino_functions',
        sa.Column('urn', sa.String(length=512), primary_key=True),
        sa.Column('function', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_trino_functions')
