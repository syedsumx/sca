"""Compliance forms and the per-asset tasks people complete

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28 04:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cairn_forms',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('spec', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=512), nullable=False),
    sa.Column('created_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cairn_form_tasks',
    sa.Column('form_id', sa.String(length=40), nullable=False),
    sa.Column('urn', sa.String(length=512), nullable=False),
    sa.Column('assignees', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('answers', sa.Text(), nullable=True),
    sa.Column('completed_by', sa.String(length=512), nullable=True),
    sa.Column('completed_ms', sa.BigInteger(), nullable=True),
    sa.Column('verified_by', sa.String(length=512), nullable=True),
    sa.Column('verified_ms', sa.BigInteger(), nullable=True),
    sa.Column('domain', sa.String(length=512), nullable=True),
    sa.Column('assigned_ms', sa.BigInteger(), nullable=False),
    sa.Column('updated_ms', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('form_id', 'urn')
    )
    with op.batch_alter_table('cairn_form_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cairn_form_tasks_state'), ['state'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('cairn_form_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cairn_form_tasks_state'))
    op.drop_table('cairn_form_tasks')
    op.drop_table('cairn_forms')
