"""One-time sign-in codes for Trino's OAuth2 flow

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-01 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_trino_oauth_codes',
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=256), nullable=False),
    sa.Column('redirect_uri', sa.String(length=1024), nullable=False),
    sa.Column('challenge', sa.String(length=128), nullable=False),
    sa.Column('method', sa.String(length=8), nullable=False),
    sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    sa.Column('used', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('code_hash')
    )


def downgrade() -> None:
    op.drop_table('cairn_trino_oauth_codes')
