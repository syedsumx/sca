"""dbt: each run (command, environment, status, counts), the last successful manifest per environment
(for --defer --state), and the published docs site

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-02 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_dbt_runs',
        sa.Column('invocation', sa.String(length=64), primary_key=True),
        sa.Column('project', sa.String(length=256), nullable=False),
        sa.Column('environment', sa.String(length=64), nullable=False),
        sa.Column('command', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=True),
        sa.Column('finished_ms', sa.BigInteger(), nullable=False),
        sa.Column('elapsed_s', sa.Integer(), nullable=True),
        sa.Column('nodes', sa.Integer(), nullable=False),
        sa.Column('errors', sa.Integer(), nullable=False),
        sa.Column('tests_failed', sa.Integer(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
    )
    op.create_index('ix_cairn_dbt_runs_project', 'cairn_dbt_runs', ['project'])
    op.create_table(
        'cairn_dbt_state',
        sa.Column('project', sa.String(length=256), primary_key=True),
        sa.Column('environment', sa.String(length=64), primary_key=True),
        sa.Column('invocation', sa.String(length=64), nullable=False),
        sa.Column('saved_ms', sa.BigInteger(), nullable=False),
        sa.Column('manifest_gz', sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        'cairn_dbt_docs',
        sa.Column('project', sa.String(length=256), primary_key=True),
        sa.Column('published_ms', sa.BigInteger(), nullable=False),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('index_gz', sa.LargeBinary(), nullable=False),
        sa.Column('manifest_gz', sa.LargeBinary(), nullable=False),
        sa.Column('catalog_gz', sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_dbt_docs')
    op.drop_table('cairn_dbt_state')
    op.drop_index('ix_cairn_dbt_runs_project', table_name='cairn_dbt_runs')
    op.drop_table('cairn_dbt_runs')
