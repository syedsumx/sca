"""Security posture: the scans and their findings

Revision ID: 0109
Revises: 0108
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0109'
down_revision = '0108'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_posture_scans',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('findings', sa.Text(), nullable=False),
        sa.Column('checks', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_posture_scans_ts_ms', 'cairn_posture_scans', ['ts_ms'])


def downgrade() -> None:
    op.drop_index('ix_cairn_posture_scans_ts_ms', table_name='cairn_posture_scans')
    op.drop_table('cairn_posture_scans')
