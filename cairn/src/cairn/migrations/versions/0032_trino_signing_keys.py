"""The key Cairn signs Trino sign-in tokens with

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-01 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_trino_signing_keys',
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('kid', sa.String(length=64), nullable=False),
    sa.Column('private_pem', sa.Text(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('cairn_trino_signing_keys')
