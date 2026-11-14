"""Infrastructure-as-code change requests: proposed by agents and people, approved, applied as the approver

Revision ID: 0133
Revises: 0132
Create Date: 2026-11-09 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0133'
down_revision = '0132'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_iac_changes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('author', sa.String(length=256), nullable=False),
        sa.Column('agent', sa.String(length=64), nullable=True),
        sa.Column('sponsor', sa.String(length=256), nullable=False),
        sa.Column('intent', sa.Text(), nullable=False),
        sa.Column('bundle', sa.Text(), nullable=False),
        sa.Column('terraform', sa.Text(), nullable=False),
        sa.Column('allow_delete', sa.Boolean(), nullable=False),
        sa.Column('plan', sa.Text(), nullable=False),
        sa.Column('plan_digest', sa.String(length=64), nullable=False),
        sa.Column('kinds', sa.Text(), nullable=False),
        sa.Column('approved_by', sa.String(length=256), nullable=True),
        sa.Column('approved_ms', sa.BigInteger(), nullable=True),
        sa.Column('approved_digest', sa.String(length=64), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('applied_by', sa.String(length=256), nullable=True),
        sa.Column('applied_ms', sa.BigInteger(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('history', sa.Text(), nullable=False),
        sa.Column('commit', sa.Text(), nullable=True),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_iac_changes_status', 'cairn_iac_changes', ['status'])
    op.create_index('ix_cairn_iac_changes_author', 'cairn_iac_changes', ['author'])
    op.create_index('ix_cairn_iac_changes_sponsor', 'cairn_iac_changes', ['sponsor'])


def downgrade() -> None:
    op.drop_index('ix_cairn_iac_changes_sponsor', table_name='cairn_iac_changes')
    op.drop_index('ix_cairn_iac_changes_author', table_name='cairn_iac_changes')
    op.drop_index('ix_cairn_iac_changes_status', table_name='cairn_iac_changes')
    op.drop_table('cairn_iac_changes')
