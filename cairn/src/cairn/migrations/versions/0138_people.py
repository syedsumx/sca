"""People & roles: the people and groups Cairn manages by hand (deployments without SCIM)

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-10 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0138'
down_revision = '0137'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_people',
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'id'),
    )


def downgrade() -> None:
    op.drop_table('cairn_people')
