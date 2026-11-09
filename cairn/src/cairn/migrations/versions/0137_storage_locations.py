"""Storage credentials and external locations: the customer's own storage, governed

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-10 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0137'
down_revision = '0136'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_storage_credentials',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=8), nullable=False),
        sa.Column('config', sa.Text(), nullable=False),
        sa.Column('secret_ref', sa.String(length=512), nullable=True),
        sa.Column('comment', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_table(
        'cairn_external_locations',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('url', sa.String(length=1024), nullable=False),
        sa.Column('credential', sa.String(length=64), nullable=False),
        sa.Column('read_only', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
        sa.UniqueConstraint('url'),
    )
    op.create_index('ix_cairn_external_locations_credential', 'cairn_external_locations', ['credential'])
    op.create_table(
        'cairn_external_location_grants',
        sa.Column('location', sa.String(length=64), nullable=False),
        sa.Column('principal', sa.String(length=256), nullable=False),
        sa.Column('privilege', sa.String(length=16), nullable=False),
        sa.Column('granted_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('location', 'principal', 'privilege'),
    )


def downgrade() -> None:
    op.drop_table('cairn_external_location_grants')
    op.drop_index('ix_cairn_external_locations_credential', table_name='cairn_external_locations')
    op.drop_table('cairn_external_locations')
    op.drop_table('cairn_storage_credentials')
