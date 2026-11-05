"""Remote agents rotate their keys: the previous signing key and when the keys changed

Revision ID: 0089
Revises: 0088
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0089'
down_revision = '0088'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_agents') as b:
        b.add_column(sa.Column('previous_signing_public', sa.String(length=128), nullable=True))
        b.add_column(sa.Column('keys_rotated_at_ms', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_agents') as b:
        b.drop_column('keys_rotated_at_ms')
        b.drop_column('previous_signing_public')
