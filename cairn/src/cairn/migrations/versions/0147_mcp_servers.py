"""External MCP servers registered in Cairn's AI gateway (L155)

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-12 16:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0147'
down_revision = '0146'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_mcp_servers',
        sa.Column('name', sa.String(40), primary_key=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('auth', sa.Text(), nullable=False),
        sa.Column('allow_tools', sa.Text(), nullable=True),
        sa.Column('deny_tools', sa.Text(), nullable=True),
        sa.Column('read_only', sa.Boolean(), nullable=False),
        sa.Column('rpm', sa.Integer(), nullable=True),
        sa.Column('team', sa.String(512), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('owner', sa.String(512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_mcp_servers')
