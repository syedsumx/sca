"""Records management: legal holds, retention runs and signed erasure proofs

Revision ID: 0082
Revises: 0081
Create Date: 2026-11-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0082'
down_revision = '0081'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_legal_holds',
        sa.Column('id', sa.String(length=24), primary_key=True),
        sa.Column('dataset', sa.String(length=700), nullable=False),
        sa.Column('case_ref', sa.String(length=200), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('placed_by', sa.String(length=512), nullable=False),
        sa.Column('placed_ms', sa.BigInteger(), nullable=False),
        sa.Column('released_by', sa.String(length=512), nullable=True),
        sa.Column('released_ms', sa.BigInteger(), nullable=True),
        sa.Column('release_note', sa.Text(), nullable=True),
    )
    op.create_index('ix_cairn_legal_holds_dataset', 'cairn_legal_holds', ['dataset'])
    op.create_table(
        'cairn_retention_runs',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('dataset', sa.String(length=700), nullable=False),
        sa.Column('policy', sa.String(length=40), nullable=False),
        sa.Column('cutoff_ms', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('deleted', sa.Integer(), nullable=True),
        sa.Column('detail', sa.String(length=500), nullable=False),
        sa.Column('ran_ms', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_cairn_retention_runs_dataset', 'cairn_retention_runs', ['dataset'])
    op.create_index('ix_cairn_retention_runs_ran_ms', 'cairn_retention_runs', ['ran_ms'])
    op.create_table(
        'cairn_erasure_proofs',
        sa.Column('request', sa.String(length=40), primary_key=True),
        sa.Column('jws', sa.Text(), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('signed_ms', sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cairn_erasure_proofs')
    op.drop_index('ix_cairn_retention_runs_ran_ms', table_name='cairn_retention_runs')
    op.drop_index('ix_cairn_retention_runs_dataset', table_name='cairn_retention_runs')
    op.drop_table('cairn_retention_runs')
    op.drop_index('ix_cairn_legal_holds_dataset', table_name='cairn_legal_holds')
    op.drop_table('cairn_legal_holds')
