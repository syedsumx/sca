"""Sources: Cairn Core ingestion sources managed from the product.

A source is a recipe (built from the connector gallery's form), a schedule (cron and time zone) and
an executor: ``default`` (Cairn Core's executor) or an in-VPC agent pool. Runs, their logs and their
reports come from Cairn Core's execution requests; agent pools fill those in as they run.

**Test connection** checks the recipe without ingesting anything, on the executor that will run it:
Cairn Core's test-connection request, or a ``test`` task for the pool's agents.

**Health**: ``healthy``, ``running``, ``failing`` (the last run failed), ``stale`` (scheduled, but no
successful run for more than twice its interval, and at least a day for daily sources),
``never_run``. The notifier checks health every few minutes; entering ``failing`` or ``stale`` tells the
source's owners (``source.failing``, ``source.stale``) and coming back tells them ``source.recovered``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, select, update

from cairn.common.db import JSONText, metadata
from cairn.sources.catalog import SourceError, all_connectors, build_recipe, connector_for, form_values, secret_refs

logger = logging.getLogger(__name__)

source_meta = Table(
    "cairn_source_meta", metadata,
    Column("urn", String(300), primary_key=True),
    Column("owners", JSONText, nullable=False),               # told about failures
    Column("created_by", String(512), nullable=False),
    Column("health", String(16), nullable=False),
    Column("health_since_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

SOURCE_FIELDS = """urn name type platform { urn }
  config { recipe version executorId debugMode }
  schedule { interval timezone }"""
EXEC_FIELDS = """urn id input { requestedAt executorId source { type } }
  result { status startTimeMs durationMs report structuredReport { type serializedValue } }"""
DONE = {"SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT", "ABORTED", "UP_FOR_RETRY", "ROLLED_BACK", "ROLLING_BACK"}
FAILED = {"FAILURE", "TIMEOUT", "ABORTED"}
ACTOR = "urn:li:corpuser:cairn-sources"


class ScheduleIn(BaseModel):
    interval: str = Field(min_length=9, max_length=100, description="cron, e.g. 0 2 * * *")
    timezone: str = Field(default="UTC", max_length=64)


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connector: str = Field(description="a gallery connector id, or custom")
    values: dict[str, Any] = Field(default_factory=dict)
    advanced: dict[str, Any] = Field(default_factory=dict)
    recipe: Optional[dict[str, Any]] = Field(default=None, description="custom connector only")
    schedule: Optional[ScheduleIn] = None
    executor: str = Field(default="default", max_length=64)
    owners: list[str] = Field(default_factory=list)


class TestIn(BaseModel):
    connector: str
    values: dict[str, Any] = Field(default_factory=dict)
    advanced: dict[str, Any] = Field(default_factory=dict)
    recipe: Optional[dict[str, Any]] = None
    executor: str = "default"


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def _interval_ms(cron: str, tz: str = "UTC") -> Optional[int]:
    """The longest gap between two runs of a cron schedule (over the next ten)."""
    try:
        from croniter import croniter

        it = croniter(cron, datetime.now(timezone.utc))
        times = [it.get_next(float) for _ in range(11)]
    except (ValueError, KeyError, ImportError):
        return None
    return int(max(b - a for a, b in zip(times, times[1:])) * 1000)


def _numbers(report: Any) -> dict[str, int]:
    """What a run did, from its structured report (Cairn Core's or an agent's)."""
    out: dict[str, int] = {}

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("events_produced", "mcps_sent", "records_written") and isinstance(x, int):
                    out["events"] = max(out.get("events", 0), x)
                elif k in ("failures", "warnings") and isinstance(x, (list, dict)):
                    out[k] = out.get(k, 0) + len(x)
                elif k in ("failures", "warnings") and isinstance(x, int):
                    out[k] = out.get(k, 0) + x
                else:
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:50]:
                walk(x, depth + 1)
    walk(report)
    return out


def summarize_run(er: dict) -> dict:
    result = er.get("result") or {}
    structured = (result.get("structuredReport") or {}).get("serializedValue")
    try:
        parsed = json.loads(structured) if structured else None
    except ValueError:
        parsed = None
    status = result.get("status") or "PENDING"
    return {"urn": er["urn"], "id": er.get("id"), "status": status,
            "requested_ms": (er.get("input") or {}).get("requestedAt"),
            "executor": (er.get("input") or {}).get("executorId"),
            "started_ms": result.get("startTimeMs"), "duration_ms": result.get("durationMs"),
            **{"events": 0, "failures": 0, "warnings": 0, **_numbers(parsed)}}


def health_of(source: dict, runs: list[dict], now_ms: Optional[int] = None) -> dict:
    now = now_ms or _now()
    if not runs:
        return {"state": "never_run", "reason": "no run yet"}
    last = runs[0]
    if last["status"] not in DONE:
        return {"state": "running", "reason": f"{last['status'].lower()} since {_iso(last['started_ms'] or last['requested_ms'])}"}
    if last["status"] in FAILED:
        streak = 0
        for r in runs:
            if r["status"] not in FAILED:
                break
            streak += 1
        return {"state": "failing", "reason": f"the last {streak} run(s) failed" if streak > 1 else "the last run failed"}
    ok = next((r for r in runs if r["status"] == "SUCCESS"), None)
    schedule = source.get("schedule") or {}
    gap = _interval_ms(schedule["interval"], schedule.get("timezone") or "UTC") if schedule.get("interval") else None
    if gap and ok:
        last_ok = (ok["started_ms"] or ok["requested_ms"] or 0) + (ok["duration_ms"] or 0)
        allowed = max(2 * gap, 86_400_000 if gap >= 86_400_000 else 2 * gap)
        if now - last_ok > allowed:
            hours = (now - last_ok) // 3_600_000
            return {"state": "stale", "reason": f"no successful run for {hours} hours"}
    if ok is None:
        return {"state": "failing", "reason": "no run has succeeded"}
    return {"state": "healthy", "reason": f"last succeeded {_iso((ok['started_ms'] or ok['requested_ms']))}"}


class SourceService:
    def __init__(self, engine, graph, gateway=None, audit=None, notify=None):
        self.engine = engine
        self.graph = graph
        self.gateway = gateway
        self.audit = audit
        self.notify = notify

    # ---- reading --------------------------------------------------------------------------
    def connectors(self) -> list[dict]:
        return [c.model_dump() for c in all_connectors()]

    def executors(self) -> list[dict]:
        out = [{"id": "default", "name": "Cairn (central executor)", "online": None}]
        if self.gateway is not None:
            for p in self.gateway.list_pools():
                out.append({"id": p["name"], "name": f"Agent pool {p['name']}", "online": p.get("agents_online")})
        return out

    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        from cairn.common.core import CoreError

        try:
            return self.graph.graphql(query, variables or {}) or {}
        except CoreError as e:
            raise SourceError(f"Cairn Core refused: {str(e)[:500]}", 502) from None

    def _public(self, s: dict, runs: list[dict], meta: Optional[dict]) -> dict:
        try:
            recipe = json.loads((s.get("config") or {}).get("recipe") or "{}")
        except ValueError:
            recipe = {}
        connector, values, advanced = form_values(recipe)
        return {"urn": s["urn"], "name": s["name"], "type": s["type"], "connector": connector,
                "values": values, "advanced": advanced, "recipe": recipe, "secrets": sorted(set(secret_refs(recipe))),
                "executor": (s.get("config") or {}).get("executorId") or "default",
                "schedule": s.get("schedule") if (s.get("schedule") or {}).get("interval") else None,
                "owners": (meta or {}).get("owners") or [], "created_by": (meta or {}).get("created_by"),
                "last_run": runs[0] if runs else None, "health": health_of(s, runs)}

    def _meta(self) -> dict[str, dict]:
        with self.engine.connect() as conn:
            return {r.urn: dict(r._mapping) for r in conn.execute(select(source_meta))}

    def list(self, runs: int = 5) -> list[dict]:
        data = self._gql(f"""query($n:Int!){{ listIngestionSources(input:{{start:0, count:500}}) {{ ingestionSources {{
            {SOURCE_FIELDS} executions(start:0, count:$n) {{ executionRequests {{ {EXEC_FIELDS} }} }} }} }} }}""",
                         {"n": runs})
        meta = self._meta()
        out = []
        for s in (data.get("listIngestionSources") or {}).get("ingestionSources") or []:
            if s["urn"].startswith("urn:li:dataHubIngestionSource:datahub-"):    # built-in maintenance jobs
                continue
            history = [summarize_run(e) for e in ((s.get("executions") or {}).get("executionRequests") or [])]
            out.append(self._public(s, history, meta.get(s["urn"])))
        return sorted(out, key=lambda x: x["name"].lower())

    def get(self, urn: str, runs: int = 20) -> dict:
        data = self._gql(f"""query($u:String!, $n:Int!){{ ingestionSource(urn:$u) {{ {SOURCE_FIELDS}
            executions(start:0, count:$n) {{ executionRequests {{ {EXEC_FIELDS} }} }} }} }}""", {"u": urn, "n": runs})
        s = data.get("ingestionSource")
        if not s:
            raise SourceError("source not found", 404)
        history = [summarize_run(e) for e in ((s.get("executions") or {}).get("executionRequests") or [])]
        return {**self._public(s, history, self._meta().get(urn)), "runs": history}

    def run_log(self, run_urn: str) -> dict:
        data = self._gql(f"query($u:String!){{ executionRequest(urn:$u) {{ {EXEC_FIELDS} }} }}", {"u": run_urn})
        er = data.get("executionRequest")
        if not er:
            raise SourceError("run not found", 404)
        report = (er.get("result") or {}).get("report") or ""
        log = report
        if self.gateway is not None:                   # agent runs: the lines the agent shipped
            task = next((t for t in self.gateway.list_runs(500) if t["execution_request_urn"] == run_urn), None)
            if task is not None:
                try:
                    log = "\n".join(self.gateway.task_logs(task["task_id"])["lines"]) + ("\n\n" + report if report else "")
                except Exception:  # noqa: BLE001 - fall back to the report
                    pass
        return {**summarize_run(er), "log": log[-60_000:]}

    # ---- writing ------------------------------------------------------------------------------
    def _check_executor(self, executor: str) -> None:
        if executor != "default" and executor not in {e["id"] for e in self.executors()}:
            raise SourceError(f"no executor {executor!r}: use default or an agent pool")

    def _input(self, body: SourceIn) -> dict:
        self._check_executor(body.executor)
        if body.schedule is not None and _interval_ms(body.schedule.interval) is None:
            raise SourceError("the schedule is not a valid cron expression")
        recipe = build_recipe(body.connector, body.values, body.advanced or None, body.recipe)
        out = {"name": body.name, "type": recipe["source"]["type"],
               "config": {"recipe": json.dumps(recipe), "executorId": body.executor}}
        if body.schedule is not None:
            out["schedule"] = {"interval": body.schedule.interval, "timezone": body.schedule.timezone}
        return out

    def create(self, body: SourceIn, actor: str) -> dict:
        urn = self._gql("mutation($i: UpdateIngestionSourceInput!){ createIngestionSource(input:$i) }",
                        {"i": self._input(body)}).get("createIngestionSource")
        if not urn:
            raise SourceError("Cairn Core did not create the source", 502)
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(source_meta.insert().values(urn=urn, owners=body.owners or [actor], created_by=actor,
                                                     health="never_run", health_since_ms=now, updated_ms=now))
        self._audit("source.created", actor, urn, {"connector": body.connector, "executor": body.executor})
        return self.get(urn)

    def update(self, urn: str, body: SourceIn, actor: str) -> dict:
        self.get(urn, runs=1)
        self._gql("mutation($u:String!, $i: UpdateIngestionSourceInput!){ updateIngestionSource(urn:$u, input:$i) }",
                  {"u": urn, "i": self._input(body)})
        now = _now()
        with self.engine.begin() as conn:
            if not conn.execute(update(source_meta).where(source_meta.c.urn == urn).values(
                    owners=body.owners or [actor], updated_ms=now)).rowcount:
                conn.execute(source_meta.insert().values(urn=urn, owners=body.owners or [actor], created_by=actor,
                                                         health="never_run", health_since_ms=now, updated_ms=now))
        self._audit("source.updated", actor, urn, {"connector": body.connector, "executor": body.executor})
        return self.get(urn)

    def delete(self, urn: str, actor: str) -> None:
        self._gql("mutation($u:String!){ deleteIngestionSource(urn:$u) }", {"u": urn})
        with self.engine.begin() as conn:
            conn.execute(source_meta.delete().where(source_meta.c.urn == urn))
        self._audit("source.deleted", actor, urn, {})

    def run(self, urn: str, actor: str) -> dict:
        er = self._gql("mutation($i: CreateIngestionExecutionRequestInput!){ createIngestionExecutionRequest(input:$i) }",
                       {"i": {"ingestionSourceUrn": urn}}).get("createIngestionExecutionRequest")
        self._audit("source.run", actor, urn, {"run": er})
        return {"run": er}

    def cancel(self, urn: str, run_urn: str, actor: str) -> dict:
        self._gql("mutation($i: CancelIngestionExecutionRequestInput!){ cancelIngestionExecutionRequest(input:$i) }",
                  {"i": {"ingestionSourceUrn": urn, "executionRequestUrn": run_urn}})
        self._audit("source.cancelled", actor, urn, {"run": run_urn})
        return {"cancelled": run_urn}

    # ---- test connection ------------------------------------------------------------------------
    def test(self, body: TestIn, actor: str) -> dict:
        self._check_executor(body.executor)
        recipe = build_recipe(body.connector, body.values, body.advanced or None, body.recipe)
        if body.executor != "default":
            from cairn.agent.gateway import EnqueueRequest

            task = self.gateway.enqueue(EnqueueRequest(pool=body.executor, recipe=recipe, kind="test", max_attempts=1),
                                        actor)
            tid = f"task:{task['task_id']}"
        else:
            urn = self._gql("mutation($i: CreateTestConnectionRequestInput!){ createTestConnectionRequest(input:$i) }",
                            {"i": {"recipe": json.dumps(recipe)}}).get("createTestConnectionRequest")
            tid = f"er:{urn}"
        self._audit("source.tested", actor, None, {"type": recipe["source"]["type"], "executor": body.executor})
        return {"id": tid, "status": "PENDING"}

    def test_result(self, tid: str) -> dict:
        kind, _, ref = tid.partition(":")
        if kind == "task" and self.gateway is not None:
            t = self.gateway.get_task(ref)
            status = t["status"] if t["status"] in DONE else "RUNNING" if t["status"] != "QUEUED" else "PENDING"
            report = (t["report"] or {}).get("test_connection")
            error = (t["report"] or {}).get("error")
        elif kind == "er":
            er = (self._gql("query($u:String!){ executionRequest(urn:$u) { result { status structuredReport "
                            "{ type serializedValue } report } } }", {"u": ref}).get("executionRequest") or {})
            result = er.get("result") or {}
            status = result.get("status") or "PENDING"
            try:
                report = json.loads((result.get("structuredReport") or {}).get("serializedValue") or "null")
            except ValueError:
                report = None
            error = None if report else (result.get("report") or "")[-2000:] or None
        else:
            raise SourceError("unknown test", 404)
        connected = None
        if isinstance(report, dict):
            basic = report.get("basic_connectivity") or {}
            connected = bool(basic.get("capable")) if basic else not report.get("internal_failure")
        return {"id": tid, "status": status, "connected": connected, "report": report, "error": error}

    # ---- health ---------------------------------------------------------------------------------
    def check_health(self, now_ms: Optional[int] = None) -> dict:
        """Compare every source's health with the last check; tell owners when it changes for the worse."""
        now = now_ms or _now()
        changed = {"failing": 0, "stale": 0, "recovered": 0}
        meta = self._meta()
        sources = self.list(runs=10)
        for s in sources:
            state = s["health"]["state"]
            m = meta.get(s["urn"])
            before = m["health"] if m else "never_run"
            if m is None:
                with self.engine.begin() as conn:
                    conn.execute(source_meta.insert().values(urn=s["urn"], owners=[], created_by=ACTOR, health=state,
                                                             health_since_ms=now, updated_ms=now))
            if state == "running" or state == before:
                continue
            if m is not None:
                with self.engine.begin() as conn:
                    conn.execute(update(source_meta).where(source_meta.c.urn == s["urn"]).values(
                        health=state, health_since_ms=now))
            if state in ("failing", "stale"):
                changed[state] += 1
                self._tell(f"source.{state}", s, f"Source {s['name']} is {state}", s["health"]["reason"])
            elif state == "healthy" and before in ("failing", "stale"):
                changed["recovered"] += 1
                self._tell("source.recovered", s, f"Source {s['name']} recovered", s["health"]["reason"])
        from cairn.common import metrics

        for state in ("healthy", "running", "failing", "stale", "never_run"):
            metrics.INGESTION_SOURCES.labels(state).set(sum(1 for s in sources if s["health"]["state"] == state))
        return changed

    def _tell(self, etype: str, s: dict, title: str, body: str) -> None:
        if self.notify is None:
            return
        from cairn.notify.hub import Event

        try:
            self.notify.publish(Event(type=etype, title=title, body=body, actor=ACTOR, recipients=s["owners"],
                                      url=f"{getattr(self.notify, 'public_url', '')}/cairn/settings/sources",
                                      payload={"source": s["urn"], "health": s["health"]}))
        except Exception:  # noqa: BLE001 - health checks never fail on a notification
            logger.exception("could not notify about %s", s["urn"])

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sources", action, actor, subject, details)


__all__ = ["SourceService", "SourceIn", "TestIn", "ScheduleIn", "SourceError", "health_of", "summarize_run",
           "connector_for"]
