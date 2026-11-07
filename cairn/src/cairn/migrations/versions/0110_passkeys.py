"""Passkeys and step-up: people's passkeys, one-use challenges and step-up tokens (hashed)

Revision ID: 0110
Revises: 0109
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0110'
down_revision = '0109'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_passkeys',
        sa.Column('credential_id', sa.String(length=512), primary_key=True),
        sa.Column('user', sa.String(length=512), nullable=False),
        sa.Column('public_key', sa.Text(), nullable=False),
        sa.Column('sign_count', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_used_ms', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_cairn_passkeys_user', 'cairn_passkeys', ['user'])
    op.create_table(
        'cairn_passkey_challenges',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('user', sa.String(length=512), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('challenge', sa.String(length=128), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_step_ups',
        sa.Column('token_hash', sa.String(length=64), primary_key=True),
        sa.Column('user', sa.String(length=512), nullable=False),
        sa.Column('credential_id', sa.String(length=512), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
        sa.Column('uses', sa.Integer(), nullable=False),
    )
    op.create_index('ix_cairn_step_ups_expires_ms', 'cairn_step_ups', ['expires_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_step_ups_expires_ms', table_name='cairn_step_ups')
    op.drop_table('cairn_step_ups')
    op.drop_table('cairn_passkey_challenges')
    op.drop_index('ix_cairn_passkeys_user', table_name='cairn_passkeys')
    op.drop_table('cairn_passkeys')
