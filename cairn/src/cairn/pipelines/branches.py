"""Table branches and tags, write-audit-publish, and data diff, on Iceberg tables in the lakehouse.

* **Refs.** ``GET /v1/tables/<ns.table>/refs`` lists a table's branches and tags (``VIEW_ENTITY_PAGE`` on it).
  ``POST`` makes one (``{"name", "type": "branch" | "tag", "from": <ref or snapshot id>, "max_ref_age_days"}``) and
  ``DELETE …/refs/<name>`` removes one: both need ``MANAGE_DATASET`` on the table. ``main`` is never removed.
  Engines write to a branch as usual (Spark ``INSERT INTO t.branch_x``, pyiceberg ``append(…, branch="x")``) and
  read it with ``FOR VERSION AS OF 'x'``; it is the same table to Cairn's policies, masks and row filters.
* **Write-audit-publish.** ``POST …/refs/<branch>/publish`` with optional ``checks`` (SQL that must return no rows,
  ``{table}`` standing for the branch) runs the checks on Trino as the caller, against the branch, and only when all
  pass moves ``main`` to the branch's head: a fast-forward, refused (409) when ``main`` has moved on since the branch
  was made. Needs ``WRITE_DATASET`` on the table. Audited, with the checks' outcome.
* **Diff.** ``GET /v1/tables/<ns.table>/diff?from=<ref>&to=<ref>&key=id`` compares two refs or snapshots: the
  schema (columns added, removed, retyped) and the rows, counted and sampled, as the caller on Trino, so their masks
  and row filters decide what they see of either side. With ``key``: rows added, removed and changed by key;
  without, rows only in one side (``EXCEPT ALL``).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
VIEW, READ, WRITE, MANAGE = "VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"
MAX_SAMPLE = 100


class BranchError(Exception):
    def __init__(self, message: str, status: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.status, self.detail = status, detail


def _split(table: str) -> tuple[str, str]:
    parts = (table or "").split(".")
    if len(parts) != 2 or not all(IDENT.match(p) for p in parts):
        raise BranchError("table: namespace.table")
    return parts[0], parts[1]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Branches:
    def __init__(self, catalog_props: dict, authorize: Callable[[str, str, str], bool], urn: Callable[[str, str], str],
                 run_sql: Callable[[str, str, int], tuple[list[str], list[list[Any]], bool]], trino_catalog: str,
                 audit=None):
        """``authorize(actor, privilege, urn)``: the PDP. ``run_sql(actor, sql, limit)``: Trino as the actor.
        ``catalog_props``: pyiceberg properties of the lakehouse catalog, as Cairn (for refs only: never rows)."""
        self.catalog_props, self.authorize, self.urn, self.run_sql = catalog_props, authorize, urn, run_sql
        self.trino_catalog, self.audit = trino_catalog, audit

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-branches", **self.catalog_props)

    def _need(self, actor: str, privilege: str, ns: str, t: str) -> None:
        try:
            ok = bool(self.authorize(actor, privilege, self.urn(ns, t)))
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if not ok:
            raise BranchError(f"{privilege} on {ns}.{t} is needed", 403)

    def _table(self, ns: str, t: str):
        from pyiceberg.exceptions import NoSuchTableError

        try:
            return self._catalog().load_table((ns, t))
        except NoSuchTableError:
            raise BranchError(f"no table {ns}.{t}", 404)

    def _audit(self, action: str, actor: str, ns: str, t: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("lakehouse", action, actor, self.urn(ns, t), details)

    @staticmethod
    def _snapshot_of(table, ref: Any) -> int:
        """A ref name or a snapshot id, as a snapshot id."""
        md = table.metadata
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            sid = int(ref)
            if md.snapshot_by_id(sid) is None:
                raise BranchError(f"no snapshot {sid}", 404)
            return sid
        if not isinstance(ref, str) or not REF.match(ref):
            raise BranchError("a ref is a branch or tag name, or a snapshot id")
        r = md.refs.get(ref)
        if r is None:
            raise BranchError(f"no branch or tag {ref}", 404)
        return r.snapshot_id

    # ---- refs ---------------------------------------------------------------------------------------
    def refs(self, actor: str, table: str) -> list[dict]:
        ns, t = _split(table)
        self._need(actor, VIEW, ns, t)
        tbl = self._table(ns, t)
        out = []
        for name, r in sorted(tbl.metadata.refs.items()):
            snap = tbl.metadata.snapshot_by_id(r.snapshot_id)
            out.append({"name": name, "type": str(getattr(r.snapshot_ref_type, "value", r.snapshot_ref_type)).lower(),
                        "snapshot_id": r.snapshot_id, "timestamp_ms": snap.timestamp_ms if snap else None,
                        "max_ref_age_ms": r.max_ref_age_ms})
        return out

    def create_ref(self, actor: str, table: str, name: str, kind: str = "branch", source: Any = "main",
                   max_ref_age_days: Optional[int] = None) -> dict:
        ns, t = _split(table)
        if not isinstance(name, str) or not REF.match(name) or name == "main":
            raise BranchError("name: letters, digits, '_', '-' and '.' (not main)")
        if kind not in ("branch", "tag"):
            raise BranchError("type: branch or tag")
        age = None
        if max_ref_age_days is not None:
            if not isinstance(max_ref_age_days, int) or not 1 <= max_ref_age_days <= 36500:
                raise BranchError("max_ref_age_days: 1 to 36500")
            age = max_ref_age_days * 86_400_000
        self._need(actor, MANAGE, ns, t)
        tbl = self._table(ns, t)
        if name in tbl.metadata.refs:
            raise BranchError(f"{ns}.{t} has a ref {name}", 409)
        sid = self._snapshot_of(tbl, source if source not in (None, "") else "main")
        ms = tbl.manage_snapshots()
        (ms.create_branch(sid, name, max_ref_age_ms=age) if kind == "branch" else ms.create_tag(sid, name, max_ref_age_ms=age)).commit()
        self._audit(f"table.{kind}_created", actor, ns, t, {"ref": name, "snapshot_id": sid})
        return {"name": name, "type": kind, "snapshot_id": sid, "max_ref_age_ms": age}

    def drop_ref(self, actor: str, table: str, name: str) -> dict:
        ns, t = _split(table)
        if name == "main":
            raise BranchError("main is never removed", 409)
        self._need(actor, MANAGE, ns, t)
        tbl = self._table(ns, t)
        r = tbl.metadata.refs.get(name)
        if r is None:
            raise BranchError(f"no branch or tag {name}", 404)
        kind = str(getattr(r.snapshot_ref_type, "value", r.snapshot_ref_type)).lower()
        ms = tbl.manage_snapshots()
        (ms.remove_tag(name) if kind == "tag" else ms.remove_branch(name)).commit()
        self._audit(f"table.{kind}_removed", actor, ns, t, {"ref": name, "snapshot_id": r.snapshot_id})
        return {"removed": name}

    # ---- write-audit-publish ------------------------------------------------------------------------
    def _relation(self, ns: str, t: str, snapshot_id: int) -> str:
        return f"{_q(self.trino_catalog)}.{_q(ns)}.{_q(t)} FOR VERSION AS OF {int(snapshot_id)}"

    def publish(self, actor: str, table: str, branch: str, checks: Optional[list] = None) -> dict:
        ns, t = _split(table)
        self._need(actor, WRITE, ns, t)
        checks = list(checks or [])
        if len(checks) > 50 or not all(isinstance(c, str) and "{table}" in c and len(c) <= 20_000 for c in checks):
            raise BranchError("checks: up to 50 SQL queries, each naming {table} (the branch), returning no rows to pass")
        tbl = self._table(ns, t)
        r = tbl.metadata.refs.get(branch)
        if r is None or str(getattr(r.snapshot_ref_type, "value", r.snapshot_ref_type)).lower() != "branch" or branch == "main":
            raise BranchError(f"no branch {branch} to publish", 404)
        head = r.snapshot_id
        main = tbl.metadata.refs.get("main")
        if main is not None and not self._descends(tbl, head, main.snapshot_id):
            raise BranchError(f"main has moved on since {branch} was made: publish needs main to be an ancestor of it", 409)
        outcome = []
        for c in checks:
            sql = c.replace("{table}", self._relation(ns, t, head))
            try:
                _, rows, _ = self.run_sql(actor, sql, 10)
            except Exception as e:  # noqa: BLE001 - a check that does not run does not pass
                outcome.append({"check": c, "passed": False, "error": str(e).splitlines()[0][:500] if str(e) else type(e).__name__})
                continue
            outcome.append({"check": c, "passed": not rows, "rows": len(rows)})
        if not all(o["passed"] for o in outcome):
            self._audit("table.publish_refused", actor, ns, t, {"branch": branch, "snapshot_id": head, "checks": outcome})
            msg = f"{branch} was not published: {sum(not o['passed'] for o in outcome)} check(s) failed"
            raise BranchError(msg, 409, {"message": msg, "checks": outcome})
        if main is not None and main.snapshot_id == head:
            return {"published": False, "reason": "main is already there", "snapshot_id": head, "checks": outcome}
        tbl.manage_snapshots().set_current_snapshot(snapshot_id=head).commit()
        now = self._table(ns, t).metadata.refs["main"].snapshot_id
        if now != head:
            raise BranchError("main moved while publishing; nothing was published", 409)
        self._audit("table.published", actor, ns, t, {"branch": branch, "snapshot_id": head,
                                                       "from_snapshot_id": main.snapshot_id if main else None,
                                                       "checks": outcome})
        return {"published": True, "snapshot_id": head, "checks": outcome}

    @staticmethod
    def _descends(tbl, head: int, ancestor: int) -> bool:
        sid, seen = head, set()
        while sid is not None and sid not in seen:
            if sid == ancestor:
                return True
            seen.add(sid)
            snap = tbl.metadata.snapshot_by_id(sid)
            sid = snap.parent_snapshot_id if snap is not None else None
        return False

    # ---- diff ---------------------------------------------------------------------------------------
    def diff(self, actor: str, table: str, from_ref: Any, to_ref: Any, key: Optional[list] = None,
             limit: int = 20) -> dict:
        ns, t = _split(table)
        self._need(actor, READ, ns, t)
        if not 0 <= int(limit) <= MAX_SAMPLE:
            raise BranchError(f"limit: 0 to {MAX_SAMPLE}")
        key = [k for k in (key or []) if k]
        if not all(IDENT.match(k) for k in key):
            raise BranchError("key: column names")
        tbl = self._table(ns, t)
        a, b = self._snapshot_of(tbl, from_ref), self._snapshot_of(tbl, to_ref)
        sa, sb = self._schema(tbl, a), self._schema(tbl, b)
        schema = {"added": sorted(set(sb) - set(sa)), "removed": sorted(set(sa) - set(sb)),
                  "retyped": sorted(c for c in set(sa) & set(sb) if sa[c] != sb[c])}
        cols = [c for c in sb if c in sa and sa[c] == sb[c]]
        if missing := [k for k in key if k not in cols]:
            raise BranchError(f"key: {', '.join(missing)} is not a column of both sides with one type")
        A, B = self._relation(ns, t, a), self._relation(ns, t, b)
        lst = ", ".join(_q(c) for c in cols)
        started = time.time()
        out: dict[str, Any] = {"table": f"{ns}.{t}", "from_snapshot_id": a, "to_snapshot_id": b, "schema": schema,
                               "columns": cols, "key": key}
        if key:
            on = " AND ".join(f"x.{_q(k)} = y.{_q(k)}" for k in key)
            rest = [c for c in cols if c not in key]
            differs = " OR ".join(f"x.{_q(c)} IS DISTINCT FROM y.{_q(c)}" for c in rest) or "FALSE"
            kl = ", ".join(f"x.{_q(k)}" for k in key)
            queries = {
                "added": f"SELECT {', '.join('x.' + _q(c) for c in cols)} FROM {B} x LEFT JOIN {A} y ON {on} "
                         f"WHERE y.{_q(key[0])} IS NULL",
                "removed": f"SELECT {', '.join('x.' + _q(c) for c in cols)} FROM {A} x LEFT JOIN {B} y ON {on} "
                           f"WHERE y.{_q(key[0])} IS NULL",
                "changed": f"SELECT {kl}, " + ", ".join(f"y.{_q(c)} AS {_q('before_' + c)}, x.{_q(c)} AS {_q('after_' + c)}"
                                                        for c in rest)
                           + f" FROM {B} x JOIN {A} y ON {on} WHERE {differs}" if rest else None,
            }
        else:
            queries = {"added": f"SELECT {lst} FROM {B} EXCEPT ALL SELECT {lst} FROM {A}",
                       "removed": f"SELECT {lst} FROM {A} EXCEPT ALL SELECT {lst} FROM {B}"}
        for name, q in queries.items():
            if q is None:
                out[name] = {"count": 0, "sample": []}
                continue
            _, n, _ = self.run_sql(actor, f"SELECT count(*) FROM ({q}) d", 1)
            cnames, rows, _ = self.run_sql(actor, f"SELECT * FROM ({q}) d LIMIT {int(limit)}", int(limit)) if limit else ([], [], False)
            out[name] = {"count": int(n[0][0]) if n else 0, "sample": [dict(zip(cnames, r)) for r in rows]}
        out["seconds"] = round(time.time() - started, 3)
        self._audit("table.diffed", actor, ns, t, {"from_snapshot_id": a, "to_snapshot_id": b,
                                                   **{k: out[k]["count"] for k in ("added", "removed", "changed") if k in out}})
        return out

    @staticmethod
    def _schema(tbl, snapshot_id: int) -> dict[str, str]:
        md = tbl.metadata
        snap = md.snapshot_by_id(snapshot_id)
        sid = snap.schema_id if snap is not None and snap.schema_id is not None else md.current_schema_id
        schema = next((s for s in md.schemas if s.schema_id == sid), md.schema())
        return {f.name: str(f.field_type) for f in schema.fields}


def build_branches_router(b: Branches, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query

    router = APIRouter(prefix="/v1/tables", tags=["lakehouse"])

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except BranchError as e:
            raise HTTPException(e.status, e.detail or str(e))

    @router.get("/{table}/refs")
    def refs(table: str, p=Depends(current_principal)):
        return {"refs": call(b.refs, p.urn, table)}

    @router.post("/{table}/refs", status_code=201)
    def create(table: str, name: str = Body(...), type: str = Body("branch"), source: Any = Body("main", alias="from"),
               max_ref_age_days: Optional[int] = Body(None), p=Depends(current_principal)):
        return call(b.create_ref, p.urn, table, name, type, source, max_ref_age_days)

    @router.delete("/{table}/refs/{name}")
    def drop(table: str, name: str, p=Depends(current_principal)):
        return call(b.drop_ref, p.urn, table, name)

    @router.post("/{table}/refs/{name}/publish")
    def publish(table: str, name: str, checks: Optional[list] = Body(None, embed=True),
                p=Depends(current_principal)):
        return call(b.publish, p.urn, table, name, checks)

    @router.get("/{table}/diff")
    def diff(table: str, from_: str = Query(..., alias="from"), to: str = Query("main"), key: Optional[str] = None,
             limit: int = 20, p=Depends(current_principal)):
        return call(b.diff, p.urn, table, from_, to, [k.strip() for k in (key or "").split(",") if k.strip()], limit)

    return router
