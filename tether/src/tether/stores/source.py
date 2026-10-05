"""Registry of source tables Tether may purge (an allow-list; never arbitrary SQL)."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import InstrumentedAttribute, Session

from tether.db.models import Base, Customer, SupportTicket


@dataclass(frozen=True)
class SourceTable:
    model: type[Base]
    pk: InstrumentedAttribute[Any]
    # Lower runs first during purges (children before parents, respecting FKs).
    purge_order: int
    # Column holding the subject id. A purge also sweeps rows matching the subject that
    # were never recorded in lineage, as a safety net for untracked writes.
    subject_column: InstrumentedAttribute[Any] | None = None


SOURCE_TABLES: dict[str, SourceTable] = {
    "support_tickets": SourceTable(
        SupportTicket, SupportTicket.id, purge_order=10, subject_column=SupportTicket.customer_id
    ),
    "customers": SourceTable(Customer, Customer.id, purge_order=90, subject_column=Customer.id),
}


def record_id(table: str, pk: str) -> str:
    """The SDK record id for a row: ``<table>/<pk>``."""
    return f"{table}/{pk}"


def parse_record_id(ref: str) -> tuple[str, str]:
    table, _, pk = ref.partition("/")
    return table, pk


def purge_order(ref: str) -> int:
    table, _ = parse_record_id(ref)
    entry = SOURCE_TABLES.get(table)
    return entry.purge_order if entry else 50


def delete_source_record(session: Session, ref: str) -> int:
    """Delete the row behind ``<table>/<pk>``. Returns the number of rows deleted (0 or 1)."""
    table, pk = parse_record_id(ref)
    entry = SOURCE_TABLES.get(table)
    if entry is None:
        raise KeyError(f"source table {table!r} is not registered in SOURCE_TABLES")
    result = session.execute(delete(entry.model).where(entry.pk == pk))
    return int(getattr(result, "rowcount", 0) or 0)


def sweep_subject_rows(session: Session, subject_id: str) -> dict[str, int]:
    """Delete rows tied to ``subject_id`` in every registered table (children first)."""
    swept: dict[str, int] = {}
    for name, entry in sorted(SOURCE_TABLES.items(), key=lambda kv: kv[1].purge_order):
        if entry.subject_column is None:
            continue
        result = session.execute(delete(entry.model).where(entry.subject_column == subject_id))
        n = int(getattr(result, "rowcount", 0) or 0)
        if n:
            swept[name] = n
    return swept
