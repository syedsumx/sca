"""dbt projects developed in Cairn: projects, their files and environments

Revision ID: 0168
Revises: 0167
Create Date: 2026-11-18 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0168'
down_revision = '0167'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_dbt_projects',
        sa.Column('name', sa.String(64), primary_key=True),
        sa.Column('owner', sa.String(256), nullable=False),
        sa.Column('description', sa.String(2000), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_dbt_projects_owner', 'cairn_dbt_projects', ['owner'])
    op.create_table(
        'cairn_dbt_project_files',
        sa.Column('project', sa.String(64), nullable=False),
        sa.Column('path', sa.String(512), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.String(256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('project', 'path'),
    )
    op.create_table(
        'cairn_dbt_environments',
        sa.Column('project', sa.String(64), nullable=False),
        sa.Column('name', sa.String(32), nullable=False),
        sa.Column('connection', sa.String(128), nullable=False),
        sa.Column('schema', sa.String(64), nullable=False),
        sa.Column('threads', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('project', 'name'),
    )


def downgrade() -> None:
    op.drop_table('cairn_dbt_environments')
    op.drop_table('cairn_dbt_project_files')
    op.drop_table('cairn_dbt_projects')
