"""SAML sign-in: pending requests, accepted assertion IDs (replays) and OIDC codes

Revision ID: 0079
Revises: 0078
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0079'
down_revision = '0078'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_saml_requests',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('client_id', sa.String(length=128), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=True),
        sa.Column('nonce', sa.Text(), nullable=True),
        sa.Column('code_challenge', sa.String(length=128), nullable=True),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_saml_requests_created_ms', 'cairn_saml_requests', ['created_ms'])
    op.create_table(
        'cairn_saml_assertions',
        sa.Column('id', sa.String(length=256), primary_key=True),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_saml_assertions_expires_ms', 'cairn_saml_assertions', ['expires_ms'])
    op.create_table(
        'cairn_sso_codes',
        sa.Column('code_hash', sa.String(length=64), primary_key=True),
        sa.Column('client_id', sa.String(length=128), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('claims', sa.Text(), nullable=False),
        sa.Column('code_challenge', sa.String(length=128), nullable=True),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_sso_codes_expires_ms', 'cairn_sso_codes', ['expires_ms'])


def downgrade() -> None:
    op.drop_table('cairn_sso_codes')
    op.drop_table('cairn_saml_assertions')
    op.drop_table('cairn_saml_requests')
