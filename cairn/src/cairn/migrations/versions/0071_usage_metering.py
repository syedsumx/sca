"""Usage metering: the days a tenant reported, and each tenant's usage on the control plane

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0071'
down_revision = '0070'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_metering_sent',
        sa.Column('day', sa.String(length=10), primary_key=True),
        sa.Column('sent_ms', sa.BigInteger(), nullable=False),
        sa.Column('report', sa.Text(), nullable=False),
    )
    op.create_table(
        'cairn_tenant_usage',
        sa.Column('tenant', sa.String(length=32), primary_key=True),
        sa.Column('day', sa.String(length=10), primary_key=True),
        sa.Column('metric', sa.String(length=64), primary_key=True),
        sa.Column('value', sa.BigInteger(), nullable=False),
        sa.Column('received_ms', sa.BigInteger(), nullable=False),
        sa.Column('billed_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_tenant_usage')
    op.drop_table('cairn_metering_sent')
