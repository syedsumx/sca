"""The fleet (L161): the vendor console's installs and check-ins, and an install's last check-in

Revision ID: 0150
Revises: 0149
Create Date: 2026-11-12 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0150'
down_revision = '0149'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_fleet_installs',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('customer', sa.String(200), nullable=False),
        sa.Column('channel', sa.String(32), nullable=False),
        sa.Column('licence_id', sa.String(64), nullable=True),
        sa.Column('salt', sa.String(64), nullable=False),
        sa.Column('created_by', sa.String(200), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_sent_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_seen_ms', sa.BigInteger(), nullable=True),
        sa.Column('last', sa.Text(), nullable=True),
    )
    op.create_table(
        'cairn_fleet_checkins',
        sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('install_id', sa.String(40), nullable=False),
        sa.Column('received_ms', sa.BigInteger(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_fleet_checkins_install_id', 'cairn_fleet_checkins', ['install_id'])
    op.create_table(
        'cairn_fleet_report',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sent_ms', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_fleet_report')
    op.drop_index('ix_cairn_fleet_checkins_install_id', 'cairn_fleet_checkins')
    op.drop_table('cairn_fleet_checkins')
    op.drop_table('cairn_fleet_installs')
