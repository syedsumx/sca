"""Managed NiFi: deployments and the flows Cairn deploys to them

Revision ID: 0145
Revises: 0144
Create Date: 2026-11-12 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0145'
down_revision = '0144'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_nifi_deployments',
        sa.Column('name', sa.String(length=31), nullable=False),
        sa.Column('uid', sa.String(length=16), nullable=False),
        sa.Column('runtime', sa.String(length=16), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('image', sa.String(length=255), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('ca_pem', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_table(
        'cairn_nifi_flows',
        sa.Column('name', sa.String(length=63), nullable=False),
        sa.Column('deployment', sa.String(length=31), nullable=False),
        sa.Column('owner', sa.String(length=512), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('definition', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('deployed_version', sa.Integer(), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('group_id', sa.String(length=64), nullable=True),
        sa.Column('lake_session', sa.String(length=64), nullable=True),
        sa.Column('lake_session_s', sa.BigInteger(), nullable=True),
        sa.Column('bulletin_cursor', sa.BigInteger(), nullable=True),
        sa.Column('synced_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_index('ix_cairn_nifi_flows_deployment', 'cairn_nifi_flows', ['deployment'])


def downgrade() -> None:
    op.drop_index('ix_cairn_nifi_flows_deployment', table_name='cairn_nifi_flows')
    op.drop_table('cairn_nifi_flows')
    op.drop_table('cairn_nifi_deployments')
