"""Results from other data-quality tools, shown next to Cairn's own checks.

``POST /quality/external/{tool}`` takes a tool's own output:

* ``dbt``: ``run_results.json`` with ``manifest.json`` (``{"run_results": …, "manifest": …}``); each
  test is matched to the model it tests, and its column;
* ``great_expectations``: a validation result (or a checkpoint result with several); the dataset is
  the batch's table, or ``dataset`` in the query;
* ``soda``: a Soda scan result (``soda scan … --srf``, or ``scan.get_scan_results()``);
* ``generic``: ``{"results": [{"dataset" or "table", "check", "column"?, "status", "message"?, "url"?}]}``
  for anything else. Monte Carlo and Anomalo alerts arrive this way, through a webhook template or a
  small forwarder that names the table and the check.

Each check becomes an assertion in Cairn Core, attributed to its tool, with a run event per result, so
the asset's Quality tab shows it beside Cairn's monitors; Cairn keeps the last 30 results. A dataset's
health counts a failing external check like a failing monitor, and a newly failing check tells the
dataset's owners.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, select, update

from cairn.common.db import JSONText, metadata
from cairn.common.proposals import make_mcp

external_checks = Table(
    "cairn_external_checks", metadata,
    Column("id", String(40), primary_key=True),
    Column("tool", String(32), nullable=False),
    Column("dataset", String(700), nullable=False, index=True),
    Column("name", String(500), nullable=False),
    Column("column_name", String(300), nullable=True),
    Column("status", String(8), nullable=False),          # PASS | WARN | FAIL | ERROR
    Column("message", Text, nullable=False),
    Column("url", String(1000), nullable=True),
    Column("last_ms", BigInteger, nullable=False),
    Column("history", JSONText, nullable=False),          # last 30: [{ts, status}]
)

TOOLS = {"dbt": "dbt", "great_expectations": "great-expectations", "soda": "soda", "monte_carlo": "montecarlo",
         "anomalo": "anomalo", "generic": "external"}
STATUS = {"pass": "PASS", "passed": "PASS", "success": "PASS", "ok": "PASS", "true": "PASS",
          "warn": "WARN", "warning": "WARN", "fail": "FAIL", "failed": "FAIL", "failure": "FAIL", "false": "FAIL",
          "error": "ERROR", "errored": "ERROR", "runtime error": "ERROR"}
RESULT = {"PASS": "SUCCESS", "WARN": "SUCCESS", "FAIL": "FAILURE", "ERROR": "ERROR"}
KEEP = 30


class ExternalError(ValueError):
    pass


def _status(v: Any) -> Optional[str]:
    if isinstance(v, bool):
        return "PASS" if v else "FAIL"
    return STATUS.get(str(v or "").strip().lower())


def _ms(ts: Any) -> int:
    if isinstance(ts, (int, float)):
        return int(ts if ts > 1e11 else ts * 1000)
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return int(time.time() * 1000)


def _dataset(platform: str, name: str, env: str = "PROD") -> str:
    if name.startswith("urn:li:dataset:"):
        return name
    if platform in ("snowflake", "bigquery"):
        name = name.lower()
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


# ---- parsers: each returns [{dataset, name, column, status, message, url, ts}] -------------------------
def parse_dbt(body: dict, platform: str) -> list[dict]:
    rr, manifest = body.get("run_results") or {}, body.get("manifest") or {}
    if not rr.get("results"):
        raise ExternalError("dbt: send {\"run_results\": <run_results.json>, \"manifest\": <manifest.json>}")
    nodes = {**(manifest.get("nodes") or {}), **(manifest.get("sources") or {})}
    adapter = ((manifest.get("metadata") or {}).get("adapter_type")) or platform
    ts = (rr.get("metadata") or {}).get("generated_at")
    out = []
    for r in rr["results"]:
        uid = r.get("unique_id") or ""
        if not uid.startswith("test."):
            continue
        test = nodes.get(uid) or {}
        model = next((nodes[d] for d in (test.get("depends_on") or {}).get("nodes") or []
                      if d in nodes and d.split(".")[0] in ("model", "source", "snapshot", "seed")), None)
        if model is None:
            continue
        name = ".".join(p for p in (model.get("database"), model.get("schema"),
                                    model.get("alias") or model.get("identifier") or model.get("name")) if p)
        status = _status(r.get("status")) or ("PASS" if r.get("status") == "skipped" else "ERROR")
        if r.get("status") == "skipped":
            continue
        meta = test.get("test_metadata") or {}
        out.append({"dataset": _dataset(adapter, name), "name": test.get("name") or uid.split(".")[2],
                    "column": test.get("column_name") or (meta.get("kwargs") or {}).get("column_name"),
                    "status": status, "message": (r.get("message") or "")[:2000] or
                    (f"{r.get('failures')} failing rows" if r.get("failures") else ""), "url": None, "ts": ts})
    return out


def parse_great_expectations(body: dict, platform: str, dataset: Optional[str]) -> list[dict]:
    runs = list((body.get("run_results") or {}).values()) if "run_results" in body else [body]
    out = []
    for run in runs:
        v = run.get("validation_result", run) if isinstance(run, dict) else {}
        meta = v.get("meta") or {}
        batch = meta.get("active_batch_definition") or {}
        spec = meta.get("batch_spec") or {}
        table = dataset or spec.get("table_name") or batch.get("data_asset_name") or \
            (meta.get("batch_kwargs") or {}).get("table")
        if spec.get("schema_name") and table and "." not in table:
            table = f"{spec['schema_name']}.{table}"
        if not table:
            raise ExternalError("great_expectations: the result names no table; pass ?dataset=")
        ts = (meta.get("run_id") or {}).get("run_time") if isinstance(meta.get("run_id"), dict) else None
        for r in v.get("results") or []:
            cfg = r.get("expectation_config") or {}
            kwargs = cfg.get("kwargs") or {}
            result = r.get("result") or {}
            msg = ""
            if result.get("unexpected_count"):
                msg = f"{result['unexpected_count']} unexpected value(s)"
                if result.get("partial_unexpected_list"):
                    msg += f", e.g. {', '.join(map(str, result['partial_unexpected_list'][:5]))}"
            if (r.get("exception_info") or {}).get("raised_exception"):
                status, msg = "ERROR", (r["exception_info"].get("exception_message") or "")[:2000]
            else:
                status = "PASS" if r.get("success") else "FAIL"
            name = cfg.get("expectation_type") or cfg.get("type") or "expectation"
            col = kwargs.get("column")
            out.append({"dataset": _dataset(platform, table), "name": f"{name}({col})" if col else name,
                        "column": col, "status": status, "message": msg, "url": None, "ts": ts})
    return out


def parse_soda(body: dict, platform: str) -> list[dict]:
    checks = body.get("checks")
    if checks is None:
        raise ExternalError("soda: send the scan results (with checks)")
    ts = body.get("dataTimestamp") or body.get("scanEndTimestamp")
    out = []
    for c in checks:
        table = c.get("table")
        if not table:
            continue
        status = _status(c.get("outcome")) or "ERROR"
        diag = c.get("diagnostics") or {}
        msg = f"value {diag['value']}" if "value" in diag else ""
        out.append({"dataset": _dataset(platform, ".".join(p for p in (c.get("schema"), table) if p) if
                                        "." not in table else table),
                    "name": c.get("name") or c.get("definition") or "check", "column": c.get("column"),
                    "status": status, "message": msg, "url": c.get("cloudUrl"), "ts": ts})
    return out


def parse_generic(body: dict, platform: str) -> list[dict]:
    items = body.get("results")
    if not isinstance(items, list) or not items:
        raise ExternalError('send {"results": [{"dataset" or "table", "check", "status", ...}]}')
    out = []
    for i in items:
        target = i.get("dataset") or i.get("table")
        status = _status(i.get("status"))
        if not target or not i.get("check") or status is None:
            raise ExternalError("each result needs dataset (or table), check and a status (pass, warn, fail, error)")
        out.append({"dataset": _dataset(i.get("platform") or platform, target), "name": str(i["check"])[:500],
                    "column": i.get("column"), "status": status, "message": str(i.get("message") or "")[:2000],
                    "url": i.get("url"), "ts": i.get("ts")})
    return out


class ExternalResults:
    def __init__(self, engine, graph=None, notify=None, audit=None):
        self.engine = engine
        self.graph = graph
        self.notify = notify
        self.audit = audit

    def ingest(self, tool: str, body: dict, platform: str = "snowflake", dataset: Optional[str] = None,
               actor: str = "urn:li:corpuser:cairn-quality") -> dict:
        if tool not in TOOLS:
            raise ExternalError(f"tool is one of {sorted(TOOLS)}")
        if not isinstance(body, dict):
            raise ExternalError("the body is a JSON object")
        if tool == "dbt":
            results = parse_dbt(body, platform)
        elif tool == "great_expectations":
            results = parse_great_expectations(body, platform, dataset)
        elif tool == "soda":
            results = parse_soda(body, platform)
        else:
            results = parse_generic(body, platform)
        mcps, newly_failing = [], []
        for r in results:
            cid = hashlib.sha1(f"{tool}|{r['dataset']}|{r['name']}|{r.get('column')}".encode()).hexdigest()[:40]
            ts = _ms(r.get("ts"))
            with self.engine.begin() as conn:
                row = conn.execute(select(external_checks).where(external_checks.c.id == cid)).first()
                history = ([{"ts": ts, "status": r["status"]}] + list(row.history if row else []))[:KEEP]
                values = dict(tool=tool, dataset=r["dataset"], name=r["name"], column_name=r.get("column"),
                              status=r["status"], message=r.get("message") or "", url=r.get("url"), last_ms=ts,
                              history=history)
                if row is None:
                    conn.execute(external_checks.insert().values(id=cid, **values))
                else:
                    conn.execute(update(external_checks).where(external_checks.c.id == cid).values(**values))
            if r["status"] in ("FAIL", "ERROR") and (row is None or row.status not in ("FAIL", "ERROR")):
                newly_failing.append(r)
            mcps += self._mcps(tool, cid, r, ts)
        if self.graph is not None and mcps:
            self.graph.emit(mcps)
        for r in newly_failing:
            self._tell(tool, r)
        if self.audit is not None:
            self.audit.record("quality", "quality.external_received", actor, None,
                              {"tool": tool, "results": len(results), "failing": len(newly_failing)})
        return {"tool": tool, "results": len(results), "datasets": len({r["dataset"] for r in results}),
                "failing": sum(1 for r in results if r["status"] in ("FAIL", "ERROR")), "newly_failing": len(newly_failing)}

    def _mcps(self, tool: str, cid: str, r: dict, ts: int) -> list[dict]:
        urn = f"urn:li:assertion:ext-{cid[:32]}"
        info: dict[str, Any] = {
            "type": "CUSTOM", "description": f"{r['name']} ({tool.replace('_', ' ')})",
            "customAssertion": {"type": tool.replace("_", " "), "entity": r["dataset"],
                                **({"field": f"urn:li:schemaField:({r['dataset']},{r['column']})"} if r.get("column") else {}),
                                "logic": r["name"]},
            "source": {"type": "EXTERNAL"}, "customProperties": {"tool": tool, **({"url": r["url"]} if r.get("url") else {})}}
        return [make_mcp(urn, "assertionInfo", info),
                make_mcp(urn, "dataPlatformInstance", {"platform": f"urn:li:dataPlatform:{TOOLS[tool]}"}),
                make_mcp(urn, "assertionRunEvent", {
                    "timestampMillis": ts, "runId": f"{tool}-{ts}", "asserteeUrn": r["dataset"], "assertionUrn": urn,
                    "status": "COMPLETE", "result": {"type": RESULT[r["status"]],
                                                     **({"nativeResults": {"message": r["message"][:1000]}}
                                                        if r.get("message") else {})}})]

    def _tell(self, tool: str, r: dict) -> None:
        if self.notify is None:
            return
        from cairn.notify.hub import Event

        try:
            self.notify.publish(Event(type="quality.external_failed", title=f"{tool.replace('_', ' ')} check failed: {r['name']}",
                                      body=r.get("message") or f"{r['name']} is {r['status'].lower()}",
                                      entity_urn=r["dataset"], include_owners=True, include_followers=True,
                                      payload={"tool": tool, "check": r["name"]}))
        except Exception:  # noqa: BLE001 - results are kept even if nobody could be told
            pass

    def for_dataset(self, dataset: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(external_checks).where(external_checks.c.dataset == dataset)
                                .order_by(external_checks.c.tool, external_checks.c.name)).all()
        return [{"id": r.id, "tool": r.tool, "name": r.name, "column": r.column_name, "status": r.status,
                 "message": r.message, "url": r.url, "last_ms": r.last_ms, "history": r.history} for r in rows]
