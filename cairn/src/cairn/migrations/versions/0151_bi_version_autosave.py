"""Workspaces (L163): a version saved by the editor's autosave, so a run of autosaves keeps one version, not hundreds

Revision ID: 0151
Revises: 0150
Create Date: 2026-11-13 03:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0151'
down_revision = '0150'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_bi_versions') as b:
        b.add_column(sa.Column('autosave', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('cairn_bi_versions') as b:
        b.drop_column('autosave')
