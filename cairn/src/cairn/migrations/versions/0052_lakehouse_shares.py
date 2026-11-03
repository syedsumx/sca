"""Lakehouse shares: tables shared with recipients outside the organisation, through Iceberg REST

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_lakehouse_shares',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('recipient', sa.String(length=256), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('secret_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_s', sa.BigInteger(), nullable=False),
        sa.Column('expires_s', sa.BigInteger(), nullable=False),
        sa.Column('revoked_s', sa.BigInteger(), nullable=True),
        sa.Column('revoked_by', sa.String(length=256), nullable=True),
        sa.Column('last_used_s', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_lakehouse_shares')
