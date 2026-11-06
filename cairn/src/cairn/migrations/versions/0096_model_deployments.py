"""Model deployments (managed serving) and their load samples

Revision ID: 0096
Revises: 0095
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0096'
down_revision = '0095'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_model_deployments',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=48), nullable=False, unique=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('model', sa.String(length=256), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('min_replicas', sa.Integer(), nullable=False),
        sa.Column('max_replicas', sa.Integer(), nullable=False),
        sa.Column('target_concurrency', sa.Float(), nullable=False),
        sa.Column('idle_minutes', sa.Integer(), nullable=False),
        sa.Column('replicas', sa.Integer(), nullable=False),
        sa.Column('last_request_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_model_load',
        sa.Column('deployment_id', sa.String(length=32), primary_key=True),
        sa.Column('node', sa.String(length=128), primary_key=True),
        sa.Column('sampled_ms', sa.BigInteger(), nullable=False),
        sa.Column('concurrency', sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_model_load')
    op.drop_table('cairn_model_deployments')
