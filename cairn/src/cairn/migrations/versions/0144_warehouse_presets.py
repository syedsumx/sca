"""External catalogs: warehouse presets (Snowflake Open Catalog and Horizon, Databricks Unity Catalog, Snowflake
through Trino's connector), the platform their tables are catalogued under, and an owner and domain for them

Revision ID: 0144
Revises: 0143
Create Date: 2026-11-12 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0144'
down_revision = '0143'
branch_labels = None
depends_on = None

COLUMNS = (('preset', 32), ('platform', 32), ('owner', 512), ('domain', 512), ('database_name', 256),
           ('role_name', 256))


def upgrade() -> None:
    with op.batch_alter_table('cairn_external_catalogs') as b:
        for name, length in COLUMNS:
            b.add_column(sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_external_catalogs') as b:
        for name, _ in reversed(COLUMNS):
            b.drop_column(name)
