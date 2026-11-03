"""ML models: who owns each registered model in MLflow (behind Cairn)

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_ml_models',
        sa.Column('name', sa.String(length=256), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('created_s', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_ml_models')
