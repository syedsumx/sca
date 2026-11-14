"""Dataset changes: each dataset a pipeline finished writing (OpenLineage, dbt), for orchestrators

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-02 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_dataset_changes',
        sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('urn', sa.String(length=512), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('job', sa.String(length=512), nullable=True),
        sa.Column('run', sa.String(length=256), nullable=True),
    )
    op.create_index('ix_cairn_dataset_changes_urn', 'cairn_dataset_changes', ['urn'])


def downgrade() -> None:
    op.drop_index('ix_cairn_dataset_changes_urn', table_name='cairn_dataset_changes')
    op.drop_table('cairn_dataset_changes')
