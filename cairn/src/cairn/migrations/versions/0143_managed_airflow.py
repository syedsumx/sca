"""Managed Airflow: deployments, their deploys and promotions

Revision ID: 0143
Revises: 0142
Create Date: 2026-11-10 22:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0143'
down_revision = '0142'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cairn_airflow_deployments',
        sa.Column('name', sa.String(length=31), nullable=False),
        sa.Column('uid', sa.String(length=16), nullable=False),
        sa.Column('runtime', sa.String(length=16), nullable=False),
        sa.Column('spec', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('image', sa.String(length=255), nullable=True),
        sa.Column('custom_image', sa.String(length=255), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('url', sa.String(length=512), nullable=True),
        sa.Column('dags_release', sa.String(length=40), nullable=True),
        sa.Column('ops_user', sa.Integer(), nullable=True),
        sa.Column('awake_until_ms', sa.BigInteger(), nullable=True),
        sa.Column('token_ms', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.Column('updated_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_table(
        'cairn_airflow_deploys',
        sa.Column('id', sa.String(length=24), nullable=False),
        sa.Column('deployment', sa.String(length=31), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('image', sa.String(length=255), nullable=True),
        sa.Column('bundle_key', sa.String(length=512), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('files', sa.Integer(), nullable=True),
        sa.Column('bytes', sa.BigInteger(), nullable=True),
        sa.Column('git_sha', sa.String(length=64), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=128), nullable=True),
        sa.Column('checked', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=512), nullable=False),
        sa.Column('created_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_airflow_deploys_deployment', 'cairn_airflow_deploys', ['deployment'])
    op.create_table(
        'cairn_airflow_promotions',
        sa.Column('id', sa.String(length=24), nullable=False),
        sa.Column('deploy', sa.String(length=24), nullable=False),
        sa.Column('source', sa.String(length=31), nullable=False),
        sa.Column('target', sa.String(length=31), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('requested_by', sa.String(length=512), nullable=False),
        sa.Column('requested_ms', sa.BigInteger(), nullable=False),
        sa.Column('decided_by', sa.String(length=512), nullable=True),
        sa.Column('decided_ms', sa.BigInteger(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('result_deploy', sa.String(length=24), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cairn_airflow_promotions_source', 'cairn_airflow_promotions', ['source'])
    op.create_index('ix_cairn_airflow_promotions_target', 'cairn_airflow_promotions', ['target'])


def downgrade() -> None:
    op.drop_index('ix_cairn_airflow_promotions_target', table_name='cairn_airflow_promotions')
    op.drop_index('ix_cairn_airflow_promotions_source', table_name='cairn_airflow_promotions')
    op.drop_table('cairn_airflow_promotions')
    op.drop_index('ix_cairn_airflow_deploys_deployment', table_name='cairn_airflow_deploys')
    op.drop_table('cairn_airflow_deploys')
    op.drop_table('cairn_airflow_deployments')
