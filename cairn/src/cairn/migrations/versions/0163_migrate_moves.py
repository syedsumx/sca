"""Migration from other warehouses: moves (tables copied into the lakehouse and verified)

Revision ID: 0163
Revises: 0162
Create Date: 2026-11-18 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0163'
down_revision = '0162'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_migrate_moves',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('owner', sa.String(256), nullable=False),
        sa.Column('connection', sa.String(128), nullable=False),
        sa.Column('source_catalog', sa.String(128), nullable=False),
        sa.Column('target_catalog', sa.String(128), nullable=False),
        sa.Column('tables', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_migrate_moves_owner', 'cairn_migrate_moves', ['owner'])


def downgrade() -> None:
    op.drop_index('ix_cairn_migrate_moves_owner', table_name='cairn_migrate_moves')
    op.drop_table('cairn_migrate_moves')
