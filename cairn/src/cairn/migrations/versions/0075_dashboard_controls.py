"""Interactive dashboards: filter controls on a dashboard

Revision ID: 0075
Revises: 0074
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0075'
down_revision = '0074'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_dashboards') as batch:
        batch.add_column(sa.Column('controls', sa.Text(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_dashboards') as batch:
        batch.drop_column('controls')
