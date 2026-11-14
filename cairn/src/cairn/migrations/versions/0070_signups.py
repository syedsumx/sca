"""Hosted signups and Stripe's events

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0070'
down_revision = '0069'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_signups',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('tenant', sa.String(length=32), nullable=False, unique=True),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('organization', sa.String(length=200), nullable=False),
        sa.Column('plan', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=True),
        sa.Column('token_expires_ms', sa.BigInteger(), nullable=True),
        sa.Column('stripe_customer', sa.String(length=64), nullable=True),
        sa.Column('stripe_subscription', sa.String(length=64), nullable=True),
        sa.Column('paid_until', sa.String(length=10), nullable=True),
        sa.Column('licence_id', sa.String(length=64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_signups_email', 'cairn_signups', ['email'])
    op.create_index('ix_cairn_signups_token_hash', 'cairn_signups', ['token_hash'])
    op.create_index('ix_cairn_signups_stripe_customer', 'cairn_signups', ['stripe_customer'])
    op.create_index('ix_cairn_signups_stripe_subscription', 'cairn_signups', ['stripe_subscription'])
    op.create_table(
        'cairn_stripe_events',
        sa.Column('id', sa.String(length=255), primary_key=True),
        sa.Column('type', sa.String(length=128), nullable=False),
        sa.Column('received_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_stripe_events')
    op.drop_table('cairn_signups')
