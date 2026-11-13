"""Agent kit (L164): projects, the memory spaces AI coding agents share per repository

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-13 17:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0152'
down_revision = '0151'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_agent_projects',
        sa.Column('id', sa.String(length=24), primary_key=True),
        sa.Column('name', sa.String(length=40), nullable=False, unique=True),
        sa.Column('description', sa.String(length=500), nullable=False),
        sa.Column('team', sa.String(length=512), nullable=True),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_agent_projects')
