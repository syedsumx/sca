"""Workspaces: business units with delegated admins

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-02 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_workspaces',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('doc', sa.Text(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_by', sa.String(length=256), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('cairn_workspaces')
