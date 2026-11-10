"""Observability: SLOs, alert rules and silences, alerts, incidents and their timelines, deploys

Revision ID: 0140
Revises: 0138
Create Date: 2026-11-10 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0140'
down_revision = '0138'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_obs_config',
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('doc', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.String(length=512), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('kind', 'id'),
    )
    op.create_table(
        'cairn_obs_alerts',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=160), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('labels', sa.Text(), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('started_ms', sa.BigInteger(), nullable=False),
        sa.Column('firing_ms', sa.BigInteger(), nullable=True),
        sa.Column('resolved_ms', sa.BigInteger(), nullable=True),
        sa.Column('last_eval_ms', sa.BigInteger(), nullable=False),
        sa.Column('notified_ms', sa.BigInteger(), nullable=True),
        sa.Column('silenced_by', sa.String(length=64), nullable=True),
        sa.Column('acknowledged_by', sa.String(length=512), nullable=True),
        sa.Column('acknowledged_ms', sa.BigInteger(), nullable=True),
        sa.Column('incident_id', sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_obs_alerts_source', 'cairn_obs_alerts', ['source'])
    op.create_index('ix_cairn_obs_alerts_state', 'cairn_obs_alerts', ['state'])
    op.create_index('ix_cairn_obs_alerts_incident_id', 'cairn_obs_alerts', ['incident_id'])
    op.create_index('ix_cairn_obs_alerts_source_fp', 'cairn_obs_alerts', ['source', 'fingerprint'])
    op.create_table(
        'cairn_obs_incidents',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('group_key', sa.String(length=160), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('services', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('assignee', sa.String(length=512), nullable=True),
        sa.Column('opened_by', sa.String(length=512), nullable=False),
        sa.Column('opened_ms', sa.BigInteger(), nullable=False),
        sa.Column('acknowledged_by', sa.String(length=512), nullable=True),
        sa.Column('acknowledged_ms', sa.BigInteger(), nullable=True),
        sa.Column('resolved_by', sa.String(length=512), nullable=True),
        sa.Column('resolved_ms', sa.BigInteger(), nullable=True),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_obs_incidents_group_key', 'cairn_obs_incidents', ['group_key'])
    op.create_index('ix_cairn_obs_incidents_status', 'cairn_obs_incidents', ['status'])
    op.create_table(
        'cairn_obs_incident_events',
        sa.Column('seq', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('incident_id', sa.String(length=40), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('by_agent', sa.Boolean(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('doc', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('seq'),
    )
    op.create_index('ix_cairn_obs_incident_events_incident_id', 'cairn_obs_incident_events', ['incident_id'])
    op.create_table(
        'cairn_obs_deploys',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('component', sa.String(length=128), nullable=False),
        sa.Column('version', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('url', sa.String(length=1024), nullable=True),
        sa.Column('actor', sa.String(length=512), nullable=False),
        sa.Column('ts_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_obs_deploys_component', 'cairn_obs_deploys', ['component'])
    op.create_index('ix_cairn_obs_deploys_ts_ms', 'cairn_obs_deploys', ['ts_ms'])


def downgrade() -> None:
    op.drop_table('cairn_obs_deploys')
    op.drop_table('cairn_obs_incident_events')
    op.drop_table('cairn_obs_incidents')
    op.drop_table('cairn_obs_alerts')
    op.drop_table('cairn_obs_config')
