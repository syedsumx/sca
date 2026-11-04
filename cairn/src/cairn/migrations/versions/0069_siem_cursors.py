"""SIEM export: each sink's cursor in the audit log

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0069'
down_revision = '0068'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_siem_cursors',
        sa.Column('sink', sa.String(length=64), primary_key=True),
        sa.Column('seq', sa.BigInteger(), nullable=False),
        sa.Column('sent', sa.BigInteger(), nullable=False),
        sa.Column('last_ms', sa.BigInteger(), nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('retry_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('lease_owner', sa.String(length=64), nullable=True),
        sa.Column('lease_until', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_siem_cursors')
