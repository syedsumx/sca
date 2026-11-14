"""API quotas and the IP allowlist: settings and per-minute counts

Revision ID: 0083
Revises: 0082
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0083'
down_revision = '0082'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_api_limits',
        sa.Column('id', sa.String(length=16), primary_key=True),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_api_usage',
        sa.Column('key', sa.String(length=600), nullable=False),
        sa.Column('minute', sa.BigInteger(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key', 'minute'),
    )


def downgrade() -> None:
    op.drop_table('cairn_api_usage')
    op.drop_table('cairn_api_limits')
