"""Fine-tuning: models tuned on governed data, with where their examples came from

Revision ID: 0117
Revises: 0116
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0117'
down_revision = '0116'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_finetunes',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('id', sa.String(length=32), nullable=False, unique=True),
        sa.Column('base_model', sa.String(length=200), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('sources', sa.Text(), nullable=False),
        sa.Column('examples', sa.Integer(), nullable=False),
        sa.Column('redacted', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('metrics', sa.Text(), nullable=True),
        sa.Column('lora', sa.Boolean(), nullable=False),
        sa.Column('learning_rate', sa.Float(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('cairn_finetunes')
