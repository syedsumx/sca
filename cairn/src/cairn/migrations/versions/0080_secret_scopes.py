"""Secret scopes: scopes, their access lists and encrypted secrets

Revision ID: 0080
Revises: 0079
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0080'
down_revision = '0079'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_secret_scopes',
        sa.Column('name', sa.String(length=128), primary_key=True),
        sa.Column('owner', sa.String(length=256), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_secret_acls',
        sa.Column('scope', sa.String(length=128), nullable=False),
        sa.Column('principal', sa.String(length=256), nullable=False),
        sa.Column('permission', sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint('scope', 'principal'),
    )
    op.create_table(
        'cairn_secrets',
        sa.Column('scope', sa.String(length=128), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('key_id', sa.String(length=16), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('scope', 'key'),
    )


def downgrade() -> None:
    op.drop_table('cairn_secrets')
    op.drop_table('cairn_secret_acls')
    op.drop_table('cairn_secret_scopes')
