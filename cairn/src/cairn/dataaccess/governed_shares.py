"""Governed copies: sharing a table that has a column mask or a row filter (L169).

A share hands a recipient a table's files, so a table whose files hold what a mask or filter hides is never shared as
it is (``cairn.dataaccess.shares``). A share may instead list such a table under ``governed``: Cairn then keeps a
**copy** of it for that share, with the recipient's policies already applied, and shares the copy.

* **What the copy holds.** ``SELECT`` of every column of the source, each masked column replaced by its mask and the
  rows filtered, exactly as Cairn's policy decision point answers Trino for the share's recipient
  (``share:<id>``, the same principal a share's tables are checked for): the expressions come from
  :meth:`TrinoAccess.column_mask` and :meth:`TrinoAccess.row_filters`, consent filters included. A table under an
  aggregation or projection policy is refused: only aggregates may leave it, and a copy is rows.
* **Who builds it.** Trino, as the share's maker (``CREATE OR REPLACE TABLE ... AS``), into the namespace
  ``shared_<share name>``. The maker's own masks and filters apply as well, so the copy never holds more than either
  the maker or the recipient may see. The maker needs ``MANAGE`` on the source, as for any shared table, and the right
  to create the copy.
* **When policies change.** Each copy records a fingerprint of the statement that built it. Every time the recipient
  asks for the copy, the statement is derived again from the policies as they are now; if it differs (a mask added, a
  filter changed), the copy is refused until it has been rebuilt. Fail closed: a decision that cannot be made refuses.
* **Freshness.** Copies are rebuilt every ``refresh_minutes`` (60 by default), on ``POST /v1/shares/<id>/refresh``,
  and dropped when the share is revoked or expires.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SOURCE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]*\.[A-Za-z0-9_][A-Za-z0-9_$-]*$")     # namespace.table, one level each
MAX_COLUMNS = 2000


class GovernedError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def q(name: str) -> str:
    """A Trino identifier, quoted."""
    return '"' + str(name).replace('"', '""') + '"'


def namespace_for(share_name: str) -> str:
    """The namespace a share's copies live in: ``shared_`` and the share's name, as an identifier."""
    return "shared_" + re.sub(r"[^a-z0-9_]", "_", share_name.lower())


def target_for(share_name: str, source: str) -> str:
    return f"{namespace_for(share_name)}.{source.split('.', 1)[1].lower()}"


class GovernedCopies:
    def __init__(self, access, run: Callable[[str, str], list], catalog: str = "lakehouse"):
        """``access``: Cairn's Trino policy decisions (``TrinoAccess``); ``run(actor, sql)``: runs a statement on
        Trino as that person and returns its rows; ``catalog``: the lakehouse's catalog in Trino."""
        self.access, self.run, self.catalog = access, run, catalog

    # ---- the statement ------------------------------------------------------------------------------
    def _table(self, source: str) -> dict:
        ns, name = source.split(".", 1)
        return {"catalogName": self.catalog, "schemaName": ns, "tableName": name}

    def columns(self, maker: str, source: str) -> list[list[str]]:
        """The source's columns and their Trino types, as the maker sees them."""
        ns, name = source.split(".", 1)
        rows = self.run(maker, f"SHOW COLUMNS FROM {q(self.catalog)}.{q(ns)}.{q(name)}")
        cols = [[str(r[0]), str(r[1])] for r in rows]
        if not cols or len(cols) > MAX_COLUMNS:
            raise GovernedError(f"{source} has no columns the share's maker may see")
        return cols

    def select(self, sid: str, source: str, columns: list[list[str]]) -> str:
        """The copy's ``SELECT``: the source as the share's recipient may see it. Raises when no rows may leave."""
        from cairn.dataaccess.shares import PREFIX
        from cairn.dataaccess.trino import READ
        from cairn.governance.aggregation import PolicyViolation, from_obligations

        if not SOURCE.match(source):
            raise GovernedError("a governed table is named namespace.table", 422)
        t = self._table(source)
        ctx = {"identity": {"user": f"{PREFIX}{sid}", "groups": []}}
        actor = f"urn:li:corpuser:{PREFIX}{sid}"
        d = self.access.decide(actor, [], READ, self.access.table_urn(t))
        try:
            limited = from_obligations(d.obligations) is not None
        except PolicyViolation:
            limited = True
        if limited:
            raise GovernedError(f"{source} is under an aggregation or projection policy: only aggregates may leave it, "
                                "so it is not shared, governed or not")
        exprs = []
        for name, ctype in columns:
            mask = self.access.column_mask({"context": ctx, "action": {"resource": {"column": {
                **t, "columnName": name, "columnType": ctype}}}})
            exprs.append(f"{mask['expression']} AS {q(name)}" if mask else q(name))
        filters = self.access.row_filters({"context": ctx, "action": {"resource": {"table": t}}})
        where = " AND ".join(f"({f['expression']})" for f in filters)
        ns, name = source.split(".", 1)
        return (f"SELECT {', '.join(exprs)} FROM {q(self.catalog)}.{q(ns)}.{q(name)}"
                + (f" WHERE {where}" if where else ""))

    @staticmethod
    def fingerprint(select: str) -> str:
        return hashlib.sha256(select.encode()).hexdigest()

    # ---- the copy --------------------------------------------------------------------------------------
    def build(self, maker: str, sid: str, share_name: str, source: str) -> dict:
        """Builds (or rebuilds) the copy of ``source`` for the share; the entry kept on the share."""
        if not SOURCE.match(source):
            raise GovernedError("a governed table is named namespace.table", 422)
        columns = self.columns(maker, source)
        select = self.select(sid, source, columns)
        target = target_for(share_name, source)
        ns, name = target.split(".", 1)
        self.run(maker, f"CREATE SCHEMA IF NOT EXISTS {q(self.catalog)}.{q(ns)}")
        self.run(maker, f"CREATE OR REPLACE TABLE {q(self.catalog)}.{q(ns)}.{q(name)} AS {select}")
        return {"source": source, "target": target, "columns": columns, "fingerprint": self.fingerprint(select),
                "refreshed_s": int(time.time())}

    def current(self, sid: str, entry: dict) -> bool:
        """Is the copy still what the policies say the recipient may see? Any error: no."""
        try:
            return self.fingerprint(self.select(sid, entry["source"], entry["columns"])) == entry["fingerprint"]
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("governed copy %s could not be checked: %s", entry.get("target"), e)
            return False

    def drop(self, maker: str, entry: dict) -> Optional[str]:
        """Drops the copy; the error if it could not be."""
        ns, name = entry["target"].split(".", 1)
        try:
            self.run(maker, f"DROP TABLE IF EXISTS {q(self.catalog)}.{q(ns)}.{q(name)}")
            return None
        except Exception as e:  # noqa: BLE001 - reported, tried again by the sweep
            return f"{type(e).__name__}: {e}"[:500]
