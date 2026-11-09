"""Guided experience: a person's onboarding progress and sidebar choices (profile ``ui``), and workspace UI settings

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-09 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0134'
down_revision = '0133'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_profiles') as batch:
        batch.add_column(sa.Column('ui', sa.Text(), nullable=True))
    op.create_table(
        'cairn_ui_settings',
        sa.Column('key', sa.String(length=64), primary_key=True),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('changed_by', sa.String(length=512), nullable=True),
        sa.Column('changed_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_ui_settings')
    with op.batch_alter_table('cairn_profiles') as batch:
        batch.drop_column('ui')
