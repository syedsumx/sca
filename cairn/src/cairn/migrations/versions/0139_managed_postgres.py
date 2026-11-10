"""Managed Postgres: instances run by Cairn, their backups, and the instance each database lives on

Revision ID: 0139
Revises: 0138
Create Date: 2026-11-10 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0139'
down_revision = '0138'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_pg_instances',
        sa.Column('name', sa.String(length=31), primary_key=True),
        sa.Column('uid', sa.String(length=16), nullable=False),
        sa.Column('runtime', sa.String(length=16), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('image', sa.String(length=255), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('restored_from', sa.Text(), nullable=True),
        sa.Column('pending', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_pg_backups',
        sa.Column('id', sa.String(length=48), primary_key=True),
        sa.Column('instance', sa.String(length=31), nullable=False),
        sa.Column('uid', sa.String(length=16), nullable=False),
        sa.Column('schedule', sa.String(length=31), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.Column('bytes', sa.BigInteger(), nullable=True),
        sa.Column('start_lsn', sa.String(length=32), nullable=True),
        sa.Column('end_lsn', sa.String(length=32), nullable=True),
        sa.Column('timeline', sa.Integer(), nullable=True),
        sa.Column('location', sa.String(length=512), nullable=True),
        sa.Column('verified', sa.String(length=12), nullable=True),
        sa.Column('verified_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('expired', sa.Boolean(), nullable=False),
    )
    op.create_index('ix_cairn_pg_backups_instance', 'cairn_pg_backups', ['instance'])
    with op.batch_alter_table('cairn_databases') as batch:
        batch.add_column(sa.Column('instance', sa.String(length=31), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_databases') as batch:
        batch.drop_column('instance')
    op.drop_index('ix_cairn_pg_backups_instance', table_name='cairn_pg_backups')
    op.drop_table('cairn_pg_backups')
    op.drop_table('cairn_pg_instances')
