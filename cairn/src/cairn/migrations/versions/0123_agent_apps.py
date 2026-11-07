"""Agent apps: code-first agents deployed to Cairn, and their sessions

Revision ID: 0123
Revises: 0122
Create Date: 2026-11-07 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0123'
down_revision = '0122'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_agent_apps',
        sa.Column('name', sa.String(length=48), primary_key=True),
        sa.Column('agent_id', sa.String(length=32), nullable=False),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('manifest', sa.Text(), nullable=False),
        sa.Column('bundle', sa.Text(), nullable=False),
        sa.Column('version', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_agent_app_sessions',
        sa.Column('app', sa.String(length=48), primary_key=True),
        sa.Column('actor', sa.String(length=512), primary_key=True),
        sa.Column('session_id', sa.String(length=128), primary_key=True),
        sa.Column('messages', sa.Text(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_agent_app_sessions')
    op.drop_table('cairn_agent_apps')
