"""Materialized views: a query's result kept as a lakehouse table and refreshed whenever a table it reads
changes, written in SQL.

Trino cannot create materialized views on a REST catalog, so Cairn keeps them: in the SQL editor (and
``POST /sql/run``), these statements are Cairn's:

* ``CREATE MATERIALIZED VIEW [IF NOT EXISTS] [<catalog>.]<schema>.<name> [WITH (refresh_every_minutes = N)]
  AS <select>``: a derived table (``cairn.pipelines.derived``) of kind ``materialized_view``, owned by whoever
  ran it, refreshed **on change** of any table it reads, and also every N minutes when given. It is filled
  at once; if that first fill fails, nothing is kept.
* ``REFRESH MATERIALIZED VIEW <name>``: refreshes it now (its owner, or an operator).
* ``DROP MATERIALIZED VIEW [IF EXISTS] <name>``: forgets it and drops its table.

A change is any commit to a table the view reads, whoever made it: Cairn Catalog reports each commit
(``POST /v1/lakehouse/changes``), besides pipes, streams, dbt, Spark and Trino's lineage. Each view says
whether it is **fresh** (nothing it reads changed since its last refresh) and, if not, what changed.

Reading one is reading its table, under the policies on that table. Its rows are what its owner's query
returned (their masks and row filters applied), so give its table the access its contents call for.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from cairn.pipelines.derived import NAME, DerivedError

IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
TARGET = rf"({IDENT}(?:\s*\.\s*{IDENT}){{1,2}})"
CREATE = re.compile(rf"^\s*CREATE\s+MATERIALIZED\s+VIEW\s+(IF\s+NOT\s+EXISTS\s+)?{TARGET}\s*"
                    rf"(?:WITH\s*\(\s*refresh_every_minutes\s*=\s*(\d+)\s*\)\s*)?AS\s+(.+)$", re.I | re.S)
REFRESH = re.compile(rf"^\s*REFRESH\s+MATERIALIZED\s+VIEW\s+{TARGET}\s*;?\s*$", re.I)
DROP = re.compile(rf"^\s*DROP\s+MATERIALIZED\s+VIEW\s+(IF\s+EXISTS\s+)?{TARGET}\s*;?\s*$", re.I)
KIND = "materialized_view"


class MaterializedViews:
    def __init__(self, derived, catalog: str, operator=None):
        """``derived``: DerivedTables; ``operator(actor)``: may this person manage anyone's views."""
        self.derived, self.catalog = derived, catalog
        self.operator = operator or (lambda actor: False)

    def _target(self, text: str) -> str:
        parts = [p.strip().strip('"') for p in re.split(r"\s*\.\s*", text.strip())]
        if len(parts) == 3:
            if parts[0].lower() != self.catalog.lower():
                raise DerivedError(f"materialized views live in the {self.catalog} catalog")
            parts = parts[1:]
        schema, name = (p.lower() for p in parts)
        if not (NAME.match(schema) and NAME.match(name)):
            raise DerivedError("a materialized view is <schema>.<name>")
        return f"{schema}.{name}"

    def _find(self, target: str) -> Optional[dict]:
        return next((d for d in self.derived.list() if d["target"] == target), None)

    def _mine(self, actor: str, target: str, if_exists: bool = False) -> Optional[dict]:
        d = self._find(target)
        if d is None or d.get("kind") != KIND or (d["owner"] != actor and not self.operator(actor)):
            if if_exists:
                return None
            raise DerivedError(f"no materialized view {self.catalog}.{target} of yours", 404)
        return d

    def handle(self, actor: str, sql: str) -> Optional[str]:
        """Runs a materialized view statement and says what it did; None if ``sql`` is not one."""
        sql = sql.strip().rstrip(";").strip()
        m = CREATE.match(sql)
        if m:
            target = self._target(m.group(2))
            existing = self._find(target)
            if existing is not None:
                if m.group(1) and existing.get("kind") == KIND:
                    return f"materialized view {self.catalog}.{target} exists"
                raise DerivedError(f"{self.catalog}.{target} is already kept by Cairn", 409)
            every = int(m.group(3)) if m.group(3) else None
            d = self.derived.create({"name": f"{self.catalog}.{target}", "sql": m.group(4).strip(), "target": target,
                                     "mode": "full", "on_change": True, "every_minutes": every, "kind": KIND}, actor, actor)
            run = self.derived.refresh(d["id"], full=True, trigger="manual")
            if run["status"] != "ok":                           # nothing half-made: no view without its table
                self.derived.delete(d["id"], actor)
                raise DerivedError(f"the materialized view could not be filled: {run['error']}", 400)
            how = "when a table it reads changes" + (f", and every {every} minutes" if every else "")
            return f"materialized view {self.catalog}.{target} created; it refreshes {how}"
        m = REFRESH.match(sql)
        if m:
            d = self._mine(actor, self._target(m.group(1)))
            run = self.derived.refresh(d["id"], trigger="manual")
            if run["status"] != "ok":
                raise DerivedError(f"refreshing {self.catalog}.{d['target']} failed: {run['error']}", 400)
            return f"materialized view {self.catalog}.{d['target']} refreshed"
        m = DROP.match(sql)
        if m:
            d = self._mine(actor, self._target(m.group(2)), if_exists=bool(m.group(1)))
            if d is None:
                return "nothing to drop"
            schema, table = d["target"].split(".")
            self.derived.run(d["owner"], f'DROP TABLE IF EXISTS "{self.catalog}"."{schema}"."{table}"')
            self.derived.delete(d["id"], actor)
            return f"materialized view {self.catalog}.{d['target']} dropped"
        return None

    def result(self, message: str, started: float) -> dict:
        """The SQL editor's result for a statement Cairn ran itself."""
        return {"columns": ["result"], "rows": [[message]], "masked_columns": [], "truncated": False,
                "elapsed_ms": int((time.monotonic() - started) * 1000), "tables": []}
