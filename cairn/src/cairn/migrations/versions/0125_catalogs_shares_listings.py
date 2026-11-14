"""External Iceberg catalogs, share usage and marketplace listings of agents and skills

Revision ID: 0125
Revises: 0124
Create Date: 2026-11-08 06:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0125'
down_revision = '0124'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_external_catalogs',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('uri', sa.String(length=1024), nullable=False),
        sa.Column('warehouse', sa.String(length=512), nullable=True),
        sa.Column('auth', sa.String(length=16), nullable=False),
        sa.Column('client_id', sa.String(length=256), nullable=True),
        sa.Column('credential_ref', sa.String(length=512), nullable=True),
        sa.Column('token_url', sa.String(length=1024), nullable=True),
        sa.Column('oauth_scope', sa.String(length=256), nullable=True),
        sa.Column('store_endpoint', sa.String(length=1024), nullable=True),
        sa.Column('store_region', sa.String(length=64), nullable=True),
        sa.Column('writable', sa.Boolean(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.Column('trino_state', sa.String(length=16), nullable=False),
        sa.Column('trino_error', sa.Text(), nullable=True),
        sa.Column('last_sync_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_sync', sa.Text(), nullable=True),
    )
    op.create_table(
        'cairn_share_usage',
        sa.Column('share_id', sa.String(length=32), nullable=False),
        sa.Column('day', sa.Integer(), nullable=False),
        sa.Column('table_name', sa.String(length=512), nullable=False),
        sa.Column('requests', sa.BigInteger(), nullable=False),
        sa.Column('loads', sa.BigInteger(), nullable=False),
        sa.Column('refusals', sa.BigInteger(), nullable=False),
        sa.Column('failures', sa.BigInteger(), nullable=False),
        sa.Column('tokens', sa.BigInteger(), nullable=False),
        sa.Column('last_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('share_id', 'day', 'table_name'),
    )
    op.create_table(
        'cairn_share_notices',
        sa.Column('share_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('sent_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('share_id', 'kind'),
    )
    op.create_table(
        'cairn_marketplace_listings',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('tags', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('approvers', sa.Text(), nullable=False),
        sa.Column('purposes', sa.Text(), nullable=False),
        sa.Column('max_duration_days', sa.Integer(), nullable=False),
        sa.Column('requires_justification', sa.Boolean(), nullable=False),
        sa.Column('persona_id', sa.String(length=64), nullable=False),
        sa.Column('published', sa.Boolean(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('kind', 'target', name='uq_cairn_marketplace_listing_target'),
    )


def downgrade() -> None:
    op.drop_table('cairn_marketplace_listings')
    op.drop_table('cairn_share_notices')
    op.drop_table('cairn_share_usage')
    op.drop_table('cairn_external_catalogs')
