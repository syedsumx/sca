"""Managed Airflow: who holds a deployment's change (a lease), scheduled wakes' backoff, a manual hibernation's hold,
and one deployment per Docker port

Revision ID: 0157
Revises: 0156
Create Date: 2026-11-14 20:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0157'
down_revision = '0156'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('cairn_airflow_deployments') as b:
        b.add_column(sa.Column('lease_ms', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('hold_until_ms', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('wake_failures', sa.Integer(), nullable=True))
        b.add_column(sa.Column('wake_retry_ms', sa.BigInteger(), nullable=True))
    # Two deployments given the same port (a race this revision closes): only one of them could have started, the
    # other failed; it keeps no port, so the index below can be made.
    conn = op.get_bind()
    t = sa.table('cairn_airflow_deployments', sa.column('name'), sa.column('port'), sa.column('created_ms'))
    seen: set[int] = set()
    for name, port in conn.execute(sa.select(t.c.name, t.c.port).where(t.c.port.isnot(None))
                                   .order_by(t.c.created_ms, t.c.name)).all():
        if port in seen:
            conn.execute(sa.update(t).where(t.c.name == name).values(port=None))
        seen.add(port)
    op.create_index('ux_cairn_airflow_deployments_port', 'cairn_airflow_deployments', ['port'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_cairn_airflow_deployments_port', table_name='cairn_airflow_deployments')
    with op.batch_alter_table('cairn_airflow_deployments') as b:
        b.drop_column('wake_retry_ms')
        b.drop_column('wake_failures')
        b.drop_column('hold_until_ms')
        b.drop_column('lease_ms')
