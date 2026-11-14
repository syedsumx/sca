"""Consent: purposes that need it, and each subject's consent as a hash

Revision ID: 0098
Revises: 0097
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0098'
down_revision = '0097'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_consent_purposes',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('identifier', sa.String(length=64), nullable=False),
        sa.Column('mode', sa.String(length=8), nullable=False),
        sa.Column('members', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_consents',
        sa.Column('purpose', sa.String(length=64), primary_key=True),
        sa.Column('subject_hash', sa.String(length=64), primary_key=True),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('source', sa.String(length=120), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_consents_updated_ms', 'cairn_consents', ['updated_ms'])


def downgrade() -> None:
    op.drop_table('cairn_consents')
    op.drop_table('cairn_consent_purposes')
