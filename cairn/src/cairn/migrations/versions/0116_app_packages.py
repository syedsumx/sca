"""App packages: versions released from git, and installs bound to the installer's datasets

Revision ID: 0116
Revises: 0115
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0116'
down_revision = '0115'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_app_packages',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('git', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_app_package_versions',
        sa.Column('package', sa.String(length=64), primary_key=True),
        sa.Column('version', sa.String(length=32), primary_key=True),
        sa.Column('commit', sa.String(length=64), nullable=False),
        sa.Column('manifest', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_app_installs',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('package', sa.String(length=64), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('references', sa.Text(), nullable=False),
        sa.Column('installed_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_app_installs_package', 'cairn_app_installs', ['package'])


def downgrade() -> None:
    op.drop_index('ix_cairn_app_installs_package', table_name='cairn_app_installs')
    op.drop_table('cairn_app_installs')
    op.drop_table('cairn_app_package_versions')
    op.drop_table('cairn_app_packages')
