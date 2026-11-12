"""The release feed an install follows: the newest index serial it accepted, and its last check (L159)

Revision ID: 0148
Revises: 0147
Create Date: 2026-11-12 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0148'
down_revision = '0147'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_release_state',
        sa.Column('channel', sa.String(32), primary_key=True),
        sa.Column('serial', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('checked_ms', sa.BigInteger(), nullable=False),
        sa.Column('ok_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_release_state')
