"""Streams: Avro messages (Confluent wire format, schemas from a Schema Registry)

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_streams') as b:
        b.add_column(sa.Column('encoding', sa.String(length=16), nullable=False, server_default='json'))


def downgrade() -> None:
    with op.batch_alter_table('cairn_streams') as b:
        b.drop_column('encoding')
