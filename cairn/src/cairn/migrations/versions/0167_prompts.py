"""A prompt registry: prompts, their versions and aliases

Revision ID: 0167
Revises: 0166
Create Date: 2026-11-18 07:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0167'
down_revision = '0166'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_prompts',
        sa.Column('name', sa.String(64), primary_key=True),
        sa.Column('owner', sa.String(256), nullable=False),
        sa.Column('description', sa.String(2000), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_prompts_owner', 'cairn_prompts', ['owner'])
    op.create_table(
        'cairn_prompt_versions',
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('template', sa.Text(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('note', sa.String(500), nullable=False),
        sa.Column('created_by', sa.String(256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name', 'version'),
    )
    op.create_table(
        'cairn_prompt_aliases',
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('alias', sa.String(32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('set_by', sa.String(256), nullable=False),
        sa.Column('set_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name', 'alias'),
    )


def downgrade() -> None:
    op.drop_table('cairn_prompt_aliases')
    op.drop_table('cairn_prompt_versions')
    op.drop_table('cairn_prompts')
