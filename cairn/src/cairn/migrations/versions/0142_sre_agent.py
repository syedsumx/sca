"""The SRE agent: its runbooks and settings, runs, proposed remediations and postmortems

Revision ID: 0142
Revises: 0140
Create Date: 2026-11-10 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0142'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_sre_config',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'id'),
    )
    op.create_table(
        'cairn_sre_runs',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('incident_id', sa.String(length=40), nullable=True),
        sa.Column('instance', sa.String(length=31), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('acts_for', sa.String(length=512), nullable=False),
        sa.Column('asked_by', sa.String(length=512), nullable=True),
        sa.Column('question', sa.Text(), nullable=True),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('steps', sa.Text(), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_sre_runs_incident_id', 'cairn_sre_runs', ['incident_id'])
    op.create_index('ix_cairn_sre_runs_started_ms', 'cairn_sre_runs', ['started_ms'])
    op.create_table(
        'cairn_sre_actions',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('incident_id', sa.String(length=40), nullable=True),
        sa.Column('run_id', sa.String(length=40), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=True),
        sa.Column('params', sa.Text(), nullable=False),
        sa.Column('change_id', sa.String(length=32), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('proposed_ms', sa.BigInteger(), nullable=False),
        sa.Column('decided_by', sa.String(length=512), nullable=True),
        sa.Column('decided_ms', sa.BigInteger(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_sre_actions_incident_id', 'cairn_sre_actions', ['incident_id'])
    op.create_index('ix_cairn_sre_actions_status', 'cairn_sre_actions', ['status'])
    op.create_table(
        'cairn_sre_postmortems',
        sa.Column('incident_id', sa.String(length=40), nullable=False),
        sa.Column('markdown', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('run_id', sa.String(length=40), nullable=True),
        sa.Column('drafted_ms', sa.BigInteger(), nullable=False),
        sa.Column('edited_by', sa.String(length=512), nullable=True),
        sa.Column('edited_ms', sa.BigInteger(), nullable=True),
        sa.Column('approved_by', sa.String(length=512), nullable=True),
        sa.Column('approved_ms', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('incident_id'),
    )


def downgrade() -> None:
    op.drop_table('cairn_sre_postmortems')
    op.drop_index('ix_cairn_sre_actions_status', table_name='cairn_sre_actions')
    op.drop_index('ix_cairn_sre_actions_incident_id', table_name='cairn_sre_actions')
    op.drop_table('cairn_sre_actions')
    op.drop_index('ix_cairn_sre_runs_started_ms', table_name='cairn_sre_runs')
    op.drop_index('ix_cairn_sre_runs_incident_id', table_name='cairn_sre_runs')
    op.drop_table('cairn_sre_runs')
    op.drop_table('cairn_sre_config')
