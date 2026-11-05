"""Embedded dashboards: used token ids and embed sessions

Revision ID: 0086
Revises: 0085
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0086'
down_revision = '0085'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_embed_nonces',
        sa.Column('client', sa.String(length=128), primary_key=True),
        sa.Column('jti', sa.String(length=128), primary_key=True),
        sa.Column('exp', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_embed_nonces_exp', 'cairn_embed_nonces', ['exp'])
    op.create_table(
        'cairn_embed_sessions',
        sa.Column('id_hash', sa.String(length=64), primary_key=True),
        sa.Column('client', sa.String(length=128), nullable=False),
        sa.Column('sub', sa.String(length=512), nullable=False),
        sa.Column('dashboard', sa.String(length=64), nullable=False),
        sa.Column('origin', sa.String(length=512), nullable=False),
        sa.Column('exp', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_embed_sessions_exp', 'cairn_embed_sessions', ['exp'])


def downgrade() -> None:
    op.drop_index('ix_cairn_embed_sessions_exp', table_name='cairn_embed_sessions')
    op.drop_table('cairn_embed_sessions')
    op.drop_index('ix_cairn_embed_nonces_exp', table_name='cairn_embed_nonces')
    op.drop_table('cairn_embed_nonces')
