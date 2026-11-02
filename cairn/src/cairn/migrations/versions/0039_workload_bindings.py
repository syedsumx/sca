"""Workload identity: an issuer and a token subject (with optional claims) bound to a Cairn user

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-02 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_workload_bindings',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('issuer', sa.String(length=64), nullable=False),
        sa.Column('subject', sa.String(length=512), nullable=False),
        sa.Column('conditions', sa.Text(), nullable=False),
        sa.Column('user', sa.String(length=256), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('issuer', 'subject', 'conditions', name='uq_cairn_workload_binding'),
    )


def downgrade() -> None:
    op.drop_table('cairn_workload_bindings')
