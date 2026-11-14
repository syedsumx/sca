"""SCIM provisioning: identity-provider metadata for provisioned users and groups

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 23:02:35.920587
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op



revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_scim_resources',
    sa.Column('resource_type', sa.String(length=8), nullable=False),
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('external_id', sa.String(length=512), nullable=True),
    sa.Column('display', sa.String(length=512), nullable=True),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('modified_ms', sa.BigInteger(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('resource_type', 'id')
    )
    with op.batch_alter_table('cairn_scim_resources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_scim_resources_external_id'), ['external_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('cairn_scim_resources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_scim_resources_external_id'))

    op.drop_table('cairn_scim_resources')
