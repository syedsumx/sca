"""Split control and data plane (data planes, their connectors, calls through the tunnel, compute per workspace) and
egress rules for user code

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-09 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0136'
down_revision = '0135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_dataplanes',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('description', sa.String(length=1000), nullable=False),
        sa.Column('connection', sa.String(length=128), nullable=True),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_table(
        'cairn_dataplane_enrollment_tokens',
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('dataplane', sa.String(length=64), nullable=False),
        sa.Column('expires_at_ms', sa.BigInteger(), nullable=False),
        sa.Column('used_by', sa.String(length=64), nullable=True),
        sa.Column('created_by', sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint('token_hash'),
    )
    op.create_index('ix_cairn_dataplane_enrollment_tokens_dataplane', 'cairn_dataplane_enrollment_tokens', ['dataplane'])
    op.create_table(
        'cairn_dataplane_connectors',
        sa.Column('connector_id', sa.String(length=64), nullable=False),
        sa.Column('dataplane', sa.String(length=64), nullable=False),
        sa.Column('hostname', sa.String(length=255), nullable=False),
        sa.Column('version', sa.String(length=64), nullable=False),
        sa.Column('signing_public', sa.String(length=128), nullable=False),
        sa.Column('encryption_public', sa.String(length=128), nullable=False),
        sa.Column('previous_signing_public', sa.String(length=128), nullable=True),
        sa.Column('keys_rotated_at_ms', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('capabilities', sa.Text(), nullable=True),
        sa.Column('enrolled_at_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_seen_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_ip', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('connector_id'),
    )
    op.create_index('ix_cairn_dataplane_connectors_dataplane', 'cairn_dataplane_connectors', ['dataplane'])
    op.create_table(
        'cairn_dataplane_calls',
        sa.Column('call_id', sa.String(length=64), nullable=False),
        sa.Column('dataplane', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('request', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('connector_id', sa.String(length=64), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('actor', sa.String(length=256), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('deadline_ms', sa.BigInteger(), nullable=False),
        sa.Column('leased_ms', sa.BigInteger(), nullable=True),
        sa.Column('finished_ms', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('call_id'),
    )
    op.create_index('ix_cairn_dataplane_calls_dataplane', 'cairn_dataplane_calls', ['dataplane'])
    op.create_index('ix_cairn_dataplane_calls_status', 'cairn_dataplane_calls', ['status'])
    op.create_index('ix_cairn_dataplane_calls_created_ms', 'cairn_dataplane_calls', ['created_ms'])
    op.create_table(
        'cairn_dataplane_nonces',
        sa.Column('nonce', sa.String(length=64), nullable=False),
        sa.Column('connector_id', sa.String(length=64), nullable=False),
        sa.Column('seen_at_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('nonce'),
    )
    op.create_index('ix_cairn_dataplane_nonces_seen_at_ms', 'cairn_dataplane_nonces', ['seen_at_ms'])
    op.create_table(
        'cairn_workspace_compute',
        sa.Column('workspace', sa.String(length=64), nullable=False),
        sa.Column('compute', sa.String(length=16), nullable=False),
        sa.Column('dataplane', sa.String(length=64), nullable=True),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('workspace'),
    )
    op.create_table(
        'cairn_egress_rules',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('workspace', sa.String(length=64), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=True),
        sa.Column('cidr', sa.String(length=64), nullable=True),
        sa.Column('ports', sa.Text(), nullable=False),
        sa.Column('description', sa.String(length=1000), nullable=False),
        sa.Column('updated_by', sa.String(length=256), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_index('ix_cairn_egress_rules_workspace', 'cairn_egress_rules', ['workspace'])


def downgrade() -> None:
    op.drop_index('ix_cairn_egress_rules_workspace', table_name='cairn_egress_rules')
    op.drop_table('cairn_egress_rules')
    op.drop_table('cairn_workspace_compute')
    op.drop_index('ix_cairn_dataplane_nonces_seen_at_ms', table_name='cairn_dataplane_nonces')
    op.drop_table('cairn_dataplane_nonces')
    for ix in ('created_ms', 'status', 'dataplane'):
        op.drop_index(f'ix_cairn_dataplane_calls_{ix}', table_name='cairn_dataplane_calls')
    op.drop_table('cairn_dataplane_calls')
    op.drop_index('ix_cairn_dataplane_connectors_dataplane', table_name='cairn_dataplane_connectors')
    op.drop_table('cairn_dataplane_connectors')
    op.drop_index('ix_cairn_dataplane_enrollment_tokens_dataplane', table_name='cairn_dataplane_enrollment_tokens')
    op.drop_table('cairn_dataplane_enrollment_tokens')
    op.drop_table('cairn_dataplanes')
