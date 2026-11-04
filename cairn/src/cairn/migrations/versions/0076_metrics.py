"""The metrics layer: named measures defined once

Revision ID: 0076
Revises: 0075
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0076'
down_revision = '0075'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_metrics',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('connection', sa.String(length=128), nullable=False),
        sa.Column('table_name', sa.String(length=512), nullable=False),
        sa.Column('measure', sa.Text(), nullable=False),
        sa.Column('filters', sa.Text(), nullable=False),
        sa.Column('time_column', sa.String(length=128), nullable=True),
        sa.Column('dimensions', sa.Text(), nullable=False),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('certified', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_metrics_owner', 'cairn_metrics', ['owner'])


def downgrade() -> None:
    op.drop_table('cairn_metrics')
