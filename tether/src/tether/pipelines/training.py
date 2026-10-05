"""Training pipeline (stub): build dataset snapshots and register "fine-tuned" models.

No real training happens. The point is to record which snapshot each model version
was trained on, so a deletion request can flag exactly the affected models.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

import tether
from tether.db.models import SupportTicket
from tether.stores.source import record_id


@dataclass(frozen=True)
class TrainingResult:
    snapshots: dict[str, str]
    models: list[str]


def _ticket_ids(session: Session, customer_ids: list[str] | None = None) -> list[str]:
    """Read tickets for the dataset and tag each one as a source record (idempotent)."""
    stmt = select(SupportTicket).order_by(SupportTicket.id)
    if customer_ids is not None:
        stmt = stmt.where(SupportTicket.customer_id.in_(customer_ids))
    ids = []
    for t in session.scalars(stmt):
        rid = record_id("support_tickets", t.id)
        tether.record_source(rid, [t.customer_id], attrs={"category": t.category}, session=session)
        ids.append(rid)
    return ids


def run_training(session: Session) -> TrainingResult:
    """Create two snapshots and three model versions.

    * ``support-tickets-full``: every ticket (restricted subjects are excluded)
    * ``support-tickets-cohort-b``: tickets from customers 26-50 only
    * ``support-assistant@1.0`` is trained on full
    * ``support-assistant@1.1`` is trained on cohort-b
    * ``ticket-router@0.3`` is trained on both
    """
    cohort_b = [f"cust-{n:03d}" for n in range(26, 51)]
    full = tether.snapshot_dataset(
        "support-tickets-full", _ticket_ids(session), snapshot_id="support-tickets-full-v1",
        session=session,
    )  # fmt: skip
    b = tether.snapshot_dataset(
        "support-tickets-cohort-b",
        _ticket_ids(session, cohort_b),
        snapshot_id="support-tickets-cohort-b-v1",
        session=session,
    )
    models = [
        tether.register_model("support-assistant", "1.0", [full], session=session),
        tether.register_model("support-assistant", "1.1", [b], session=session),
        tether.register_model("ticket-router", "0.3", [full, b], session=session),
    ]
    return TrainingResult(snapshots={"full": full, "cohort_b": b}, models=[m.id for m in models])
