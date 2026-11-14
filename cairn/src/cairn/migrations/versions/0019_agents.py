"""AI agents: registered clients (keys and OAuth) and their tokens

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-30 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_agent_clients',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('owner', sa.String(length=512), nullable=True),
    sa.Column('scopes', sa.Text(), nullable=False),
    sa.Column('redirect_uris', sa.Text(), nullable=False),
    sa.Column('ai_system', sa.String(length=40), nullable=True),
    sa.Column('key_hash', sa.String(length=64), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('last_used_ms', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_table('cairn_agent_tokens',
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=512), nullable=False),
    sa.Column('scopes', sa.Text(), nullable=False),
    sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('hash')
    )
    op.create_index('ix_cairn_agent_tokens_client_id', 'cairn_agent_tokens', ['client_id'], unique=False)
    op.create_index('ix_cairn_agent_tokens_actor', 'cairn_agent_tokens', ['actor'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_cairn_agent_tokens_actor', table_name='cairn_agent_tokens')
    op.drop_index('ix_cairn_agent_tokens_client_id', table_name='cairn_agent_tokens')
    op.drop_table('cairn_agent_tokens')
    op.drop_table('cairn_agent_clients')
