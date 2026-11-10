"""The SRE agent's Postgres helper: "why is it slow, is it healthy" for a managed instance.

Everything is read as the person the agent acts for: the instance's status, metrics, backups and slow statements
through Cairn's API (the agent's credential: its scopes and their permissions), and per database, through a
short-lived **read login** (L88: ``VIEW_DATABASE``, a read-only session, dropped as soon as the diagnosis ends):

* top slow statements (``pg_stat_statements``), each planned with ``EXPLAIN (GENERIC_PLAN)`` on the read login: a
  sequential scan filtering a large table on columns no index leads with is an **index suggestion** with the plan as
  its evidence; nothing is created (an index is a ``postgres_indexes`` change request a person approves);
* bloat and vacuum hints (dead tuples, when autovacuum last ran), tables read mostly by sequential scans;
* connection saturation (and sessions idle in a transaction), replication lag, WAL archiving, backup freshness,
  the cache hit ratio;
* each problem with a remediation to propose: a change request (an index, a parameter, a size) or a runbook action
  (a restart, a backup).

SQL the model writes (:func:`run_diagnostic_sql`) is one read-only statement (``SELECT``, ``WITH``, ``EXPLAIN``
without ``ANALYZE``, ``SHOW``) in a read-only transaction with a statement timeout, on a read login."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MIN_ROWS = 10_000              # a table smaller than this is scanned faster than an index is read
MAX_SELECTIVITY = 0.05         # the scan keeps at most this share of the table's rows
DEAD_RATIO = 0.2
STATEMENT_TIMEOUT = "5s"
MAX_ROWS = 50
READ_START = re.compile(r"^\s*(select|with|explain|show|table|values)\b", re.I)


class DiagnosticError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def check_instance(name: Any) -> str:
    s = str(name or "")
    if not INSTANCE.match(s):
        raise DiagnosticError(f"no instance {s!r}", 404)
    return s


def check_database(name: Any) -> str:
    s = str(name or "")
    if not DATABASE.match(s):
        raise DiagnosticError(f"no database {s!r}", 404)
    return s


def _clip(v: Any, n: int = 300) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    if isinstance(v, (list, tuple)):
        return [_clip(x, n) for x in v]
    if isinstance(v, dict):
        return {k: _clip(x, n) for k, x in v.items()}
    return v


# ---- plans --------------------------------------------------------------------------------------------------------
def _nodes(plan: dict):
    yield plan
    for p in plan.get("Plans") or []:
        yield from _nodes(p)


_COMPARE = re.compile(r"\(*\b([a-z_][a-z0-9_]*)\b\)*(?:::[a-z ]+(?:\[\])?)?\s*(=|<=|>=|<|>|~~|= ANY)\s*")


def filter_columns(text: str) -> tuple[list[str], list[str]]:
    """(columns compared for equality, columns compared otherwise) in a plan node's Filter."""
    eq: list[str] = []
    other: list[str] = []
    for col, op_ in _COMPARE.findall(text or ""):
        if col in ("and", "or", "not", "true", "false", "null"):
            continue
        (eq if op_.startswith("=") else other).append(col)
    dedup = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return dedup(eq), [c for c in dedup(other) if c not in eq]


def generic_plan(conn, query: str) -> dict:
    """The generic plan of a normalised statement (``$1`` for its constants): planned, never run, in a read-only
    transaction."""
    if ";" in query.strip().rstrip(";"):
        raise DiagnosticError("one statement only")
    conn.autocommit = False
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("EXPLAIN (GENERIC_PLAN, FORMAT JSON) " + query.strip().rstrip(";")).fetchone()
    finally:
        conn.rollback()
        conn.autocommit = True
    doc = row[0]
    if isinstance(doc, str):
        doc = json.loads(doc)
    return doc[0]["Plan"]


def index_suggestions(conn, database: str, statement: dict) -> tuple[list[dict], dict]:
    """Index suggestions for one slow statement, validated against its generic plan on the read login, and the plan's
    summary (the evidence)."""
    query = str(statement.get("query") or "")
    if not re.match(r"^\s*(select|update|delete|with)\b", query, re.I):
        return [], {"skipped": "only SELECT, UPDATE, DELETE and WITH statements are planned"}
    plan = generic_plan(conn, query)
    summary = {"node": plan.get("Node Type"), "total_cost": plan.get("Total Cost"), "scans": []}
    out: list[dict] = []
    for n in _nodes(plan):
        if n.get("Node Type") not in ("Seq Scan", "Parallel Seq Scan") or not n.get("Filter"):
            continue
        schema, table = n.get("Schema") or "public", n.get("Relation Name")
        scan = {"table": f"{schema}.{table}", "filter": n.get("Filter"), "estimated_rows": n.get("Plan Rows"),
                "cost": n.get("Total Cost")}
        summary["scans"].append(scan)
        stats = conn.execute(
            "SELECT c.reltuples::bigint, coalesce(s.seq_scan, 0), coalesce(s.idx_scan, 0) FROM pg_class c "
            "JOIN pg_namespace ns ON ns.oid = c.relnamespace LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
            "WHERE ns.nspname = %s AND c.relname = %s", (schema, table)).fetchone()
        if not stats:
            continue
        rows = max(int(stats[0] or 0), 0)
        scan["table_rows"] = rows
        eq, other = filter_columns(n["Filter"])
        cols = {r[0] for r in conn.execute(
            "SELECT a.attname::text FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace ns "
            "ON ns.oid = c.relnamespace WHERE ns.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT "
            "a.attisdropped", (schema, table)).fetchall()}
        wanted = [c for c in eq if c in cols][:2] or [c for c in other if c in cols][:1]
        if not wanted:
            continue
        selectivity = (n.get("Plan Rows") or 0) / rows if rows else 1.0
        leading = {r[0] for r in conn.execute(
            "SELECT a.attname::text FROM pg_index ix JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_namespace ns "
            "ON ns.oid = t.relnamespace JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0] "
            "WHERE ns.nspname = %s AND t.relname = %s", (schema, table)).fetchall()}
        why_not = None
        if rows < MIN_ROWS:
            why_not = f"the table has about {rows} rows: a scan is cheap"
        elif selectivity > MAX_SELECTIVITY:
            why_not = f"the filter keeps about {selectivity:.0%} of the rows: an index would not be used"
        elif wanted[0] in leading:
            why_not = f"an index already leads with {wanted[0]}"
        name = re.sub(r"[^a-z0-9_]", "_", f"{table}_{'_'.join(wanted)}_idx")[:63]
        s = {"database": database, "table": f"{schema}.{table}", "columns": wanted, "index": name,
             "estimated_rows": n.get("Plan Rows"), "table_rows": rows, "selectivity": round(selectivity, 6),
             "plan_node": n.get("Node Type"), "filter": n.get("Filter"), "plan_cost": n.get("Total Cost"),
             "seq_scans": stats[1], "index_scans": stats[2], "queryid": statement.get("queryid"),
             "mean_ms": statement.get("mean_ms"), "calls": statement.get("calls"),
             "validated": why_not is None, "why_not": why_not}
        if why_not is None:
            s["bundle_yaml"] = (f"postgres_indexes:\n  - database: {database}\n    name: {name}\n"
                                f"    table: {schema}.{table}\n    columns: [{', '.join(wanted)}]\n"
                                "    method: btree\n    unique: false\n")
        out.append(s)
    return out, summary


def vacuum_hints(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT schemaname, relname, n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_autoanalyze, "
            "seq_scan, coalesce(idx_scan, 0) FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20").fetchall():
        live, dead = int(r[2] or 0), int(r[3] or 0)
        ratio = dead / (live + dead) if live + dead else 0.0
        last = max([t for t in (r[4], r[5]) if t is not None], default=None)
        hint = None
        if dead >= 1000 and ratio >= DEAD_RATIO:
            hint = (f"{dead} dead rows ({ratio:.0%}): VACUUM (ANALYZE) {r[0]}.{r[1]}, and check autovacuum keeps up "
                    "(autovacuum_work_mem, long transactions holding it back)")
        out.append({"table": f"{r[0]}.{r[1]}", "live_rows": live, "dead_rows": dead, "dead_ratio": round(ratio, 4),
                    "last_vacuum": last.isoformat() if last else None,
                    "last_analyze": r[6].isoformat() if r[6] else None, "seq_scans": r[7], "index_scans": r[8],
                    "hint": hint})
    return out


def run_diagnostic_sql(conn, sql_text: str) -> dict:
    """One read-only statement, in a read-only transaction with a timeout; at most MAX_ROWS rows, values clipped."""
    text = (sql_text or "").strip().rstrip(";").strip()
    if not text or len(text) > 8000:
        raise DiagnosticError("one SQL statement, up to 8,000 characters")
    if ";" in text:
        raise DiagnosticError("one statement only")
    if not READ_START.match(text):
        raise DiagnosticError("only SELECT, WITH, EXPLAIN, SHOW, TABLE or VALUES")
    head = re.split(r"\b(?:select|with|insert|update|delete)\b", text, maxsplit=1, flags=re.I)[0]
    if re.match(r"^\s*explain\b", text, re.I) and re.search(r"\banaly[sz]e\b", head, re.I):
        raise DiagnosticError("EXPLAIN without ANALYZE (it would run the statement)")
    conn.autocommit = False
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        cur = conn.execute(text)
        cols = [d.name for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1) if cur.description else []
    finally:
        conn.rollback()
        conn.autocommit = True
    return {"columns": cols, "rows": [[_clip(v if isinstance(v, (int, float, bool)) or v is None else str(v), 200)
                                       for v in r] for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS}


# ---- the diagnosis -----------------------------------------------------------------------------------------------
class ReadLogin:
    """A short-lived read login to one database, as a person; dropped when the block ends."""

    def __init__(self, databases, person: str, database: str):
        self.dbs, self.person, self.database = databases, person, database
        self.creds: Optional[dict] = None
        self.conn = None

    def __enter__(self):
        import psycopg

        if self.dbs is None:
            raise DiagnosticError("operational databases are not set up here", 503)
        try:
            self.creds = self.dbs.credentials(self.person, self.database, "read", 5)
        except Exception as e:  # noqa: BLE001 - a DatabaseError: its status and message
            raise DiagnosticError(f"no read login to {self.database}: {e}", getattr(e, "status", 403)) from e
        c = self.creds
        try:
            self.conn = psycopg.connect(host=c["host"], port=c["port"], dbname=c["database"], user=c["user"],
                                        password=c["password"], sslmode=c["sslmode"], connect_timeout=10,
                                        autocommit=True, application_name="cairn-sre-agent")
        except psycopg.OperationalError as e:
            self._drop()
            raise DiagnosticError(f"{self.database} does not answer: {str(e).splitlines()[0][:200]}", 503) from e
        self.conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        return self.conn

    def _drop(self) -> None:
        if self.creds is None:
            return
        try:
            self.dbs.revoke(self.person, self.database, self.creds["user"])
        except Exception:  # noqa: BLE001 - it expires within minutes anyway
            logger.warning("the SRE agent's read login to %s could not be dropped now", self.database)
        self.creds = None

    def __exit__(self, *exc):
        try:
            if self.conn is not None:
                self.conn.close()
        finally:
            self._drop()
        return False


def _resize_yaml(inst: dict, **change: Any) -> str:
    spec = {k: inst.get(k) for k in ("name", "version", "cpu", "memory_mb", "storage_gb", "replicas", "pooler",
                                     "maintenance_window", "image")}
    spec.update(change)
    lines = ["postgres_instances:", f"  - name: {spec.pop('name')}"]
    for k, v in spec.items():
        lines.append(f"    {k}: {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def diagnose(call: Callable[[str, str], Any], databases, person: str, instance: str,
             question: str = "") -> dict:
    """The health and performance report of one instance. ``call(method, path)`` reads Cairn's API as the agent;
    ``databases`` hands out the read logins (as ``person``). Each part that cannot be read says why."""
    name = check_instance(instance)
    base = f"/v1/databases/instances/{quote(name)}"
    report: dict[str, Any] = {"instance": name, "at_ms": int(time.time() * 1000), "problems": [], "findings": [],
                              "index_suggestions": [], "vacuum": [], "suggestions": [], "errors": []}

    def read(step: str, path: str) -> Optional[Any]:
        try:
            return call("GET", path)
        except Exception as e:  # noqa: BLE001 - a CallError: what could not be read, and why
            report["errors"].append({"step": step, "error": str(e)[:400]})
            return None

    inst = read("instance", base)
    if inst is None:
        return report
    status = read("status", f"{base}/status") or {}
    metrics = read("metrics", f"{base}/metrics") or {}
    slow = (read("slow_queries", f"{base}/slow-queries?limit=10&order=total") or {}).get("queries") or []
    backups = (read("backups", f"{base}/backups") or {}).get("backups") if name != "default" else []
    report.update(health=status.get("health"), state=status.get("state"), problems=list(status.get("problems") or []),
                  connections=status.get("connections"), replication=status.get("replication") or [],
                  wal_archiving=status.get("wal_archiving"), storage=status.get("storage"),
                  last_backup=status.get("last_backup"),
                  slow_queries=[{k: q.get(k) for k in ("queryid", "database", "calls", "total_ms", "mean_ms", "max_ms",
                                                       "rows", "shared_blocks_read", "query")} for q in slow],
                  locks_waiting=metrics.get("locks_waiting"), longest_transaction_s=metrics.get("longest_transaction_s"))
    finding = report["findings"].append
    suggest = report["suggestions"].append
    conns = status.get("connections") or {}
    pct = conns.get("percent")
    if pct is not None and pct >= 80:
        idle = conns.get("idle_in_transaction") or 0
        finding({"kind": "connections", "text": f"{conns.get('total')} of {conns.get('max')} connections in use "
                 f"({pct}%), {conns.get('active')} active, {idle} idle in a transaction"})
        if idle and idle >= 0.3 * (conns.get("total") or 1):
            suggest({"priority": 1, "kind": "runbook_action", "action": "restart_instance", "params": {"instance": name},
                     "title": f"Restart {name} to end the sessions idle in a transaction",
                     "rationale": f"{idle} sessions hold transactions open and fill the connection slots"})
            suggest({"priority": 2, "kind": "change_request", "title": f"End idle transactions on {name} after 5 minutes",
                     "rationale": "sessions idle in a transaction are ended by PostgreSQL instead of holding slots",
                     "bundle_yaml": f"postgres_parameters:\n  - instance: {name}\n    name: "
                                    "idle_in_transaction_session_timeout\n    value: 5min\n"})
        elif name != "default" and conns.get("max"):
            more = min(5000, int(conns["max"]) * 2)
            suggest({"priority": 2, "kind": "change_request", "title": f"Raise max_connections of {name} to {more}",
                     "rationale": "the active sessions need more slots (a pooler is the better fix for many clients)",
                     "bundle_yaml": f"postgres_parameters:\n  - instance: {name}\n    name: max_connections\n"
                                    f"    value: \"{more}\"\n"})
    for r in report["replication"]:
        if (r.get("lag_bytes") or 0) > 64 * 1024 * 1024 or (r.get("replay_lag_s") or 0) > 30:
            finding({"kind": "replication", "text": f"standby {r.get('standby')} is {r.get('lag_bytes')} bytes, "
                     f"{r.get('replay_lag_s')} s behind"})
    wal = status.get("wal_archiving") or {}
    if wal.get("enabled") and not wal.get("ok"):
        finding({"kind": "wal_archiving", "text": f"WAL archiving is failing (last failed {wal.get('last_failed_wal')})"})
    last = status.get("last_backup")
    if name != "default":
        age_h = (report["at_ms"] - (last.get("finished_ms") or 0)) / 3_600_000 if last else None
        report["backups"] = {"count": len(backups or []), "last_age_hours": round(age_h, 1) if age_h is not None else None,
                             "schedules": [s.get("name") for s in inst.get("backup_schedules") or []]}
        stale = last is None or any("backup" in p for p in report["problems"])
        if stale:
            finding({"kind": "backups", "text": "no recent backup" if last is None else
                     f"the last backup is {age_h:.1f} hours old, beyond its schedule"})
            suggest({"priority": 5, "kind": "runbook_action", "action": "backup_instance", "params": {"instance": name},
                     "title": f"Take a base backup of {name} now", "rationale": "the newest backup is missing or stale"})
    for db in metrics.get("databases") or []:
        ratio = db.get("cache_hit_ratio")
        if ratio is not None and ratio < 0.9 and (db.get("blocks_read") or 0) > 10_000 and name != "default":
            finding({"kind": "cache", "text": f"{db['database']}: cache hit ratio {ratio:.0%}"})
            suggest({"priority": 4, "kind": "change_request", "title": f"Give {name} twice the memory",
                     "rationale": f"{db['database']} reads most blocks from disk (cache hit ratio {ratio:.0%})",
                     "bundle_yaml": _resize_yaml(inst, memory_mb=min(int(inst.get("memory_mb") or 1024) * 2, 65536))})
            break
    # Per database, through a read login: plans of the slow statements, vacuum.
    on = sorted({q.get("database") for q in slow if q.get("database") and DATABASE.match(q.get("database"))
                 and q.get("database") not in ("postgres", "template0", "template1")})
    for db in on[:3]:
        try:
            with ReadLogin(databases, person, db) as conn:
                for q in [q for q in slow if q.get("database") == db][:5]:
                    try:
                        sug, plan = index_suggestions(conn, db, q)
                    except Exception as e:  # noqa: BLE001 - one statement that cannot be planned: said, and on
                        report["errors"].append({"step": f"explain {q.get('queryid')}", "error": str(e)[:300]})
                        continue
                    q_report = next(x for x in report["slow_queries"] if x.get("queryid") == q.get("queryid"))
                    q_report["plan"] = plan
                    report["index_suggestions"] += sug
                for v in vacuum_hints(conn):
                    report["vacuum"].append({"database": db, **v})
        except DiagnosticError as e:
            report["errors"].append({"step": f"read login to {db}", "error": str(e)})
    for s in report["index_suggestions"]:
        if s["validated"]:
            finding({"kind": "index", "text": f"{s['database']}: {s['plan_node']} on {s['table']} ({s['table_rows']} "
                     f"rows) filtering {s['filter']}, about {s['estimated_rows']} rows kept, {s['mean_ms']} ms a call; "
                     f"no index leads with {s['columns'][0]}"})
            suggest({"priority": 3, "kind": "change_request",
                     "title": f"Index {s['table']} on {', '.join(s['columns'])} in {s['database']}",
                     "rationale": f"the slow statement {s['queryid']} scans all {s['table_rows']} rows of {s['table']} "
                                  f"to keep about {s['estimated_rows']}", "bundle_yaml": s["bundle_yaml"]})
    for v in report["vacuum"]:
        if v["hint"]:
            finding({"kind": "vacuum", "text": f"{v['database']}: {v['hint']}"})
    if report["errors"]:
        report["problems"].append("parts of the diagnosis could not be read: "
                                  + "; ".join(f"{e['step']}: {e['error'][:120]}" for e in report["errors"][:5]))
    report["suggestions"].sort(key=lambda x: x["priority"])      # what hurts now first; a backup last
    report["verdict"] = ("healthy" if report.get("health") == "healthy" and not report["findings"] else
                         "down" if report.get("health") == "down" else "needs attention")
    if question:
        report["question"] = question[:1000]
    return report
