"""Agent memory: template-generated summaries that remember where they came from.

* ``summary/<customer>``: one customer, derived from their profile and tickets
* ``digest/<category>/<n>``: several customers, derived from one ticket each, so they are
  multi-subject entries
"""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

import tether
from tether.db.models import Customer, LineageNode, SupportTicket
from tether.lineage.ids import NodeType
from tether.policy.enforcement import restricted_subject_ids
from tether.stores.memory import write_memory
from tether.stores.source import record_id

DIGEST_GROUP_SIZE = 4


@dataclass(frozen=True)
class MemoryResult:
    summaries: int
    digests: int


def _load(session: Session) -> tuple[dict[str, Customer], dict[str, list[SupportTicket]]]:
    blocked = restricted_subject_ids(session)
    customers = {
        c.id: c
        for c in session.scalars(select(Customer).order_by(Customer.id))
        if c.id not in blocked
    }
    tickets: dict[str, list[SupportTicket]] = defaultdict(list)
    for t in session.scalars(select(SupportTicket).order_by(SupportTicket.id)):
        if t.customer_id in customers:
            tickets[t.customer_id].append(t)
    return customers, tickets


def _summary_text(c: Customer, tickets: list[SupportTicket]) -> str:
    cats = sorted({t.category for t in tickets})
    return (
        f"{c.name} ({c.email}, {c.phone}) has opened {len(tickets)} support ticket(s) "
        f'about {", ".join(cats)}. Most recent: "{tickets[-1].title}". '
        f"Ships to {c.address}."
    )


def _write_digest(
    session: Session, memory_id: str, category: str, members: list[tuple[Customer, SupportTicket]]
) -> None:
    content = f"Recurring {category} issues: " + "; ".join(
        f"{c.name} - {t.title} ({t.id})" for c, t in members
    )
    ref = tether.track_memory_write(
        memory_id,
        [record_id("support_tickets", t.id) for _, t in members],
        attrs={"kind": "digest", "category": category},
        session=session,
    )
    write_memory(session, ref, "digest", content)


def build_memory(session: Session) -> MemoryResult:
    customers, tickets = _load(session)
    n_summaries = 0
    for cid, c in customers.items():
        own = tickets.get(cid, [])
        if not own:
            continue
        derived = [record_id("customers", cid)] + [record_id("support_tickets", t.id) for t in own]
        for rid in derived:  # make sure every input is tagged at the source
            tether.record_source(rid, [cid], session=session)
        ref = tether.track_memory_write(
            f"summary/{cid}", derived, attrs={"kind": "customer_summary"}, session=session
        )
        write_memory(session, ref, "customer_summary", _summary_text(c, own))
        n_summaries += 1

    n_digests = 0
    for category, groups in _digest_groups(customers, tickets).items():
        for i, members in enumerate(groups):
            _write_digest(session, f"digest/{category}/{i}", category, members)
            n_digests += 1
    return MemoryResult(summaries=n_summaries, digests=n_digests)


def _digest_groups(
    customers: dict[str, Customer], tickets: dict[str, list[SupportTicket]]
) -> dict[str, list[list[tuple[Customer, SupportTicket]]]]:
    by_category: dict[str, list[tuple[Customer, SupportTicket]]] = defaultdict(list)
    for cid in sorted(customers):
        seen: set[str] = set()
        for t in tickets.get(cid, []):
            if t.category not in seen:
                seen.add(t.category)
                by_category[t.category].append((customers[cid], t))
    return {
        cat: [members[i : i + DIGEST_GROUP_SIZE] for i in range(0, len(members), DIGEST_GROUP_SIZE)]
        for cat, members in sorted(by_category.items())
    }


def regenerate_flagged_memory(session: Session) -> list[str]:
    """Rebuild digests flagged ``needs_regeneration`` from the data that is still allowed.

    A digest is rebuilt from its original source tickets, minus any ticket that has since
    been purged or now belongs to a restricted subject.
    """
    blocked = restricted_subject_ids(session)
    flagged = session.scalars(
        select(LineageNode).where(
            LineageNode.node_type == NodeType.MEMORY.value,
            LineageNode.status == "needs_regeneration",
        )
    ).all()
    rebuilt: list[str] = []
    for node in flagged:
        if node.attrs.get("kind") != "digest":
            continue
        ticket_ids = [
            d.split("/", 1)[1]
            for d in node.attrs.get("derived_from", [])
            if d.startswith("source_record:support_tickets/")
        ]
        rows = session.scalars(select(SupportTicket).where(SupportTicket.id.in_(ticket_ids)))
        members = []
        for t in rows:
            cust = session.get(Customer, t.customer_id)
            if cust is not None and t.customer_id not in blocked:
                members.append((cust, t))
        if not members:
            continue
        members.sort(key=lambda m: m[0].id)
        _write_digest(session, node.ref, str(node.attrs.get("category", "misc")), members)
        rebuilt.append(node.id)
    return rebuilt
