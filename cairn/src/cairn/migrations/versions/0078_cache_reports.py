"""The shared result cache, and scheduled exports with their files

Revision ID: 0078
Revises: 0077
Create Date: 2026-11-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0078'
down_revision = '0077'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_result_cache',
        sa.Column('key', sa.String(length=64), primary_key=True),
        sa.Column('urns', sa.Text(), nullable=False),
        sa.Column('seq', sa.BigInteger(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_result_cache_expires_ms', 'cairn_result_cache', ['expires_ms'])
    op.create_table(
        'cairn_report_schedules',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('target_kind', sa.String(length=16), nullable=False),
        sa.Column('target_id', sa.String(length=32), nullable=False),
        sa.Column('format', sa.String(length=8), nullable=False),
        sa.Column('every_minutes', sa.Integer(), nullable=False),
        sa.Column('recipients', sa.Text(), nullable=False),
        sa.Column('next_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_status', sa.String(length=500), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
    )
    op.create_index('ix_cairn_report_schedules_owner', 'cairn_report_schedules', ['owner'])
    op.create_index('ix_cairn_report_schedules_next_ms', 'cairn_report_schedules', ['next_ms'])
    op.create_table(
        'cairn_report_files',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('schedule', sa.String(length=32), nullable=False),
        sa.Column('recipient', sa.String(length=256), nullable=False),
        sa.Column('filename', sa.String(length=256), nullable=False),
        sa.Column('content_type', sa.String(length=64), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('expires_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_report_files_schedule', 'cairn_report_files', ['schedule'])
    op.create_index('ix_cairn_report_files_recipient', 'cairn_report_files', ['recipient'])
    op.create_index('ix_cairn_report_files_expires_ms', 'cairn_report_files', ['expires_ms'])


def downgrade() -> None:
    op.drop_table('cairn_report_files')
    op.drop_table('cairn_report_schedules')
    op.drop_table('cairn_result_cache')
