"""Reversible tokenisation: token domains, the vault, and the columns pipes and streams tokenise

Revision ID: 0105
Revises: 0104
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_token_domains',
        sa.Column('name', sa.String(length=64), primary_key=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('wrapped_key', sa.Text(), nullable=False),
        sa.Column('key_id', sa.String(length=16), nullable=False),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'cairn_token_vault',
        sa.Column('domain', sa.String(length=64), nullable=False),
        sa.Column('token', sa.String(length=32), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('domain', 'token'),
    )
    with op.batch_alter_table('cairn_pipes') as b:
        b.add_column(sa.Column('tokenize', sa.Text(), nullable=True))
    with op.batch_alter_table('cairn_streams') as b:
        b.add_column(sa.Column('tokenize', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cairn_streams') as b:
        b.drop_column('tokenize')
    with op.batch_alter_table('cairn_pipes') as b:
        b.drop_column('tokenize')
    op.drop_table('cairn_token_vault')
    op.drop_table('cairn_token_domains')
