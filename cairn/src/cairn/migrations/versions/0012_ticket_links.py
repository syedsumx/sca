"""Links between Cairn requests or incidents and tickets in Jira, ServiceNow, PagerDuty, Opsgenie

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-28 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_ticket_links',
    sa.Column('system_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('object_id', sa.String(length=512), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('system_id', 'external_id')
    )
    with op.batch_alter_table('cairn_ticket_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_ticket_links_object_id'), ['object_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_ticket_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_ticket_links_object_id'))
    op.drop_table('cairn_ticket_links')
