"""Data contracts and their published versions

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28 11:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_contracts',
    sa.Column('dataset_urn', sa.String(length=512), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('spec', sa.Text(), nullable=False),
    sa.Column('owner', sa.String(length=512), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_detail', sa.Text(), nullable=True),
    sa.Column('status_ms', sa.BigInteger(), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('dataset_urn')
    )
    op.create_table('cairn_contract_versions',
    sa.Column('dataset_urn', sa.String(length=512), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('spec', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('dataset_urn', 'version')
    )


def downgrade() -> None:
    op.drop_table('cairn_contract_versions')
    op.drop_table('cairn_contracts')
