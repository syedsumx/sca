"""The pipeline agent: writes, fixes and ships pipelines from a goal in plain words, as the person asking.

``POST /v1/pipeline-agent/tasks`` with ``{"goal": "Daily revenue per region from paid orders", "target":
"analytics.daily_revenue", "datasets": [urns], "checks": [{"name": "no_negative", "sql": "SELECT count(*) FROM
candidate WHERE revenue < 0"}]}`` (or ``{"fix": "<derived table id>", "goal": "..."}``):

1. **What may be used**: the tables the person may see (chosen as Ask-your-data chooses them, or the ``datasets``
   named), with their columns. For a fix, the derived table's definition and its latest failed runs' errors.
2. **A pipeline**, from the model as structured output: a derived table (one SELECT, how it refreshes: ``full``,
   ``append`` by a watermark or ``merge`` by key, on a schedule and/or when its inputs change) and data checks
   (each a SELECT over ``candidate`` that counts bad rows).
3. **Tried, as the person, writing nothing**: the definition is checked as a derived table would be (one SELECT,
   not reading its target, a schedule); every table it reads must be one the person may see; it is planned
   (``EXPLAIN``) and previewed on Trino as the person (their masks and row filters apply); a merge key must be
   unique and an append watermark must be a column; then the person's checks and the model's run over the result.
   Anything that fails (Trino's error, a check that found rows) goes back to the model, up to
   ``CAIRN_PIPELINE_AGENT_ATTEMPTS`` tries (3). Every try is kept as a step.
4. **Shipped only when the person approves** (``POST …/tasks/<id>/approve``): the derived table is created (or the
   broken one updated, rebuilt in full) as theirs, refreshed once on Trino as them, and the checks run on the
   table it made. The task records the outcome and gives the pipeline as code (a ``cairn bundle`` resource), for
   git. A failed refresh or check is reported, and a fix task can be asked for.

The model never runs anything: every statement is Cairn's or checked by Cairn, and runs as the person. The model
sees table and column names, the person's goal and errors (redacted), never rows. Model use is metered to the
person (AI budget). Audited: ``pipeline_agent.drafted``, ``pipeline_agent.shipped``, ``pipeline_agent.rejected``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, select, update

from cairn.common.db import metadata

agent_tasks = Table(
    "cairn_pipeline_agent_tasks", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("kind", String(8), nullable=False),          # build | fix
    Column("goal", Text, nullable=False),
    Column("target", String(512), nullable=False),
    Column("derived_id", String(32), nullable=True),
    Column("status", String(16), nullable=False),       # ready | failed | shipped | ship_failed | rejected
    Column("proposal", Text, nullable=True),
    Column("checks", Text, nullable=False, default="[]"),
    Column("steps", Text, nullable=False, default="[]"),
    Column("outcome", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

CHECK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_CHECKS = 10
PREVIEW = 20

SYSTEM = """You are a data engineer. You write one pipeline step for Cairn's lakehouse: a derived table, kept as a
table from one Trino SELECT, and data checks for it. Use only the tables and columns listed, fully qualified as
listed. The SELECT gives every output column a plain name. Choose how it refreshes: "full" (rebuilt each time),
"append" (rows above the table's maximum "watermark" column are added) or "merge" (rows replace those with the same
"key" columns). Refresh "every_minutes" (1 to 10080) and/or "on_change" (when a table it reads changes). Each check
is a SELECT over the table named candidate that returns one number: the count of rows that are wrong (0 passes),
e.g. SELECT count(*) FROM candidate WHERE amount < 0. If a previous attempt failed, the error says why: fix that.
"""

SCHEMA: dict = {
    "type": "object", "additionalProperties": False,
    "required": ["sql", "mode", "on_change", "checks", "explanation"],
    "properties": {
        "sql": {"type": "string"},
        "mode": {"type": "string", "enum": ["full", "append", "merge"]},
        "watermark": {"type": ["string", "null"]},
        "key": {"type": "array", "items": {"type": "string"}},
        "every_minutes": {"type": ["integer", "null"]},
        "on_change": {"type": "boolean"},
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "sql"],
            "properties": {"name": {"type": "string"}, "sql": {"type": "string"}}}},
        "explanation": {"type": "string"},
    },
}


class PipelineAgentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def check_sql(sql: str) -> str:
    """A check: one SELECT that reads only ``candidate`` (the pipeline's result)."""
    import sqlglot
    from sqlglot import exp

    try:
        parsed = [s for s in sqlglot.parse(sql, read="trino") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise PipelineAgentError(f"the check does not parse: {str(e).splitlines()[0]}")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        raise PipelineAgentError("a check is one SELECT")
    ctes = {c.alias_or_name.lower() for c in parsed[0].find_all(exp.CTE)}
    for t in parsed[0].find_all(exp.Table):
        if t.db or t.catalog or (t.name.lower() != "candidate" and t.name.lower() not in ctes):
            raise PipelineAgentError("a check reads only the table named candidate")
    return parsed[0].sql(dialect="trino")


class PipelineAgent:
    def __init__(self, engine, llm, model: Optional[str], derived, run: Callable[[str, str, int], tuple],
                 context: Callable[[str, str, list, Callable[[str], bool]], dict], audit=None):
        """``derived``: :class:`cairn.pipelines.derived.DerivedTables` (what is shipped); ``run(actor, sql, limit)``:
        one statement on the derived tables' Trino as ``actor`` → ``(columns, rows, truncated)``;
        ``context(actor, goal, urns, can_view)``: ``{"schema.table": [columns]}`` the person may see."""
        self.engine, self.llm, self.model, self.derived, self.run = engine, llm, model, derived, run
        self.context, self.audit = context, audit
        self.attempts = max(1, int(os.environ.get("CAIRN_PIPELINE_AGENT_ATTEMPTS", "3")))

    # ---- drafting -------------------------------------------------------------------------------------
    def create(self, actor: str, body: dict, can_view: Callable[[str], bool]) -> dict:
        from cairn.ai.guardrails import redact

        if self.llm is None:
            raise PipelineAgentError("the pipeline agent needs an LLM provider (not configured)", 503)
        if self.derived is None:
            raise PipelineAgentError("the pipeline agent ships derived tables: set CAIRN_DERIVED_CONNECTION", 503)
        if not isinstance(body, dict):
            raise PipelineAgentError("a task is an object")
        goal = str(body.get("goal") or "").strip()
        fix_id = body.get("fix")
        current = None
        if fix_id is not None:
            current = self._owned_derived(actor, str(fix_id))
            target = current["target"]
            goal = goal or "Make the failing refresh work again."
        else:
            target = str(body.get("target") or "")
            if len(target.split(".")) != 2:
                raise PipelineAgentError("target: schema.table")
        if not 3 <= len(goal) <= 2000:
            raise PipelineAgentError("goal: 3 to 2,000 characters")
        checks = self._checks(body.get("checks") or [])
        urns = body.get("datasets") or []
        if not isinstance(urns, list) or not all(isinstance(u, str) for u in urns):
            raise PipelineAgentError("datasets: a list of dataset urns")
        tables = self.context(actor, goal, urns, can_view)
        if current is not None:
            for s, t in self._reads(current["sql"]):
                ref = f"{s}.{t}"
                if ref not in tables and can_view(self.derived.urn(s, t)):
                    tables.setdefault(ref, self._columns(actor, ref))
        if not tables:
            raise PipelineAgentError("no table you may see fits the goal; name the datasets", 404)
        steps: list[dict] = []
        proposal, status = None, "failed"
        for attempt in range(1, self.attempts + 1):
            plan = self._plan(goal, target, tables, current, checks, steps)
            step: dict[str, Any] = {"attempt": attempt, "sql": plan.get("sql"), "mode": plan.get("mode"),
                                    "explanation": str(plan.get("explanation") or "")[:1000]}
            try:
                proposal, results, preview = self._try(actor, target, plan, checks, can_view, current)
            except PipelineAgentError as e:
                step["error"] = redact(str(e))[:1500]
                steps.append(step)
                continue
            step.update(checks=results, preview_rows=len(preview["rows"]))
            failed = [r for r in results if not r["passed"]]
            if failed:
                step["error"] = "; ".join(f"check {r['name']} found {r['found']} wrong rows" for r in failed)
                steps.append(step)
                continue
            steps.append(step)
            status = "ready"
            break
        tid = uuid.uuid4().hex
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(agent_tasks.insert().values(
                id=tid, owner=actor, kind="fix" if current else "build", goal=redact(goal)[:2000], target=target,
                derived_id=current["id"] if current else None, status=status,
                proposal=json.dumps(proposal) if status == "ready" else None,
                checks=json.dumps(proposal["checks"] if status == "ready" else checks), steps=json.dumps(steps),
                outcome=None, created_ms=now, updated_ms=now))
        self._audit("pipeline_agent.drafted", actor, tid, {"kind": "fix" if current else "build", "target": target,
                                                            "status": status, "attempts": len(steps)})
        out = self.get(actor, tid)
        if status == "ready":
            out["preview"] = preview                     # the person's own read; not kept
        return out

    def _checks(self, raw) -> list[dict]:
        if not isinstance(raw, list) or len(raw) > MAX_CHECKS:
            raise PipelineAgentError(f"checks: up to {MAX_CHECKS} of {{name, sql}}")
        out = []
        for c in raw:
            if not isinstance(c, dict) or not CHECK_NAME.match(str(c.get("name") or "")):
                raise PipelineAgentError("a check has a name (lowercase letters, digits and _) and a sql")
            out.append({"name": c["name"], "sql": check_sql(str(c.get("sql") or "")), "by": "person"})
        return out

    def _owned_derived(self, actor: str, did: str) -> dict:
        from cairn.pipelines.derived import DerivedError

        try:
            d = self.derived.get(did)
        except DerivedError:
            raise PipelineAgentError("no such derived table", 404)
        if d["owner"] != actor:                          # only its owner's pipeline is fixed for them
            raise PipelineAgentError("no such derived table", 404)
        return d

    def _reads(self, sql: str) -> list[tuple[str, str]]:
        from cairn.pipelines.derived import DerivedError, tables_read

        try:
            return tables_read(sql, self.derived.catalog)
        except DerivedError as e:
            raise PipelineAgentError(str(e))

    def _columns(self, actor: str, ref: str) -> list[str]:
        s, t = ref.split(".")
        try:
            cols, _, _ = self.run(actor, f'SELECT * FROM "{self.derived.catalog}"."{s}"."{t}" LIMIT 0', 1)
        except Exception:  # noqa: BLE001 - a table the person cannot read gives no columns
            return []
        return [c if isinstance(c, str) else c.get("name") for c in cols]

    def _plan(self, goal: str, target: str, tables: dict, current: Optional[dict], checks: list,
              steps: list) -> dict:
        from cairn.ai.guardrails import UNTRUSTED_NOTICE, redact, untrusted
        from cairn.ai.llm import LLMError, validate_against_schema
        from cairn.ai.usage import LLMBudgetExceeded, llm_task

        lines = [f"Goal: {goal}", f"Target table: {target} (in catalog {self.derived.catalog})", "Tables:"]
        for ref, cols in tables.items():
            lines.append(f"- {self.derived.catalog}.{ref} ({', '.join(cols)})")
        if current is not None:
            lines.append(f"The current pipeline: mode {current['mode']}, watermark {current['watermark']}, key "
                         f"{','.join(current['key'])}, SQL: {current['sql']}")
            for r in [r for r in self.derived.runs(current["id"], 5) if r["status"] == "error"][:3]:
                lines.append(f"A refresh failed: {redact(str(r['error']))[:600]}")
        for c in checks:
            lines.append(f"The person's check {c['name']}: {c['sql']}")
        for s in steps:
            lines.append(f"Attempt {s['attempt']} was: {s.get('sql')} -> failed: {s.get('error')}")
        try:
            with llm_task("pipeline.agent"):
                plan, _ = self.llm.generate_json(SYSTEM + UNTRUSTED_NOTICE, untrusted("\n".join(lines)), SCHEMA,
                                                 model=self.model)
            validate_against_schema(plan, SCHEMA)
        except LLMBudgetExceeded as e:
            raise PipelineAgentError(f"AI budget reached: {e}", 429)
        except LLMError as e:
            raise PipelineAgentError(f"the model failed: {e}", 502)
        except ValueError as e:
            raise PipelineAgentError(f"the model's plan was not a pipeline: {e}", 502)
        return plan

    def _try(self, actor: str, target: str, plan: dict, checks: list, can_view, current) -> tuple[dict, list, dict]:
        """The plan, checked and run as the person, writing nothing."""
        from cairn.pipelines.derived import DerivedError

        name = current["name"] if current else re.sub(r"[^A-Za-z0-9_]", "_", target.replace(".", "_"))[:120]
        body = {"name": name, "target": target, "sql": str(plan.get("sql") or ""), "mode": plan.get("mode"),
                "watermark": plan.get("watermark") or None, "key": plan.get("key") or [],
                "every_minutes": plan.get("every_minutes"), "on_change": bool(plan.get("on_change"))}
        try:
            row = self.derived._values(body, actor, actor)            # as a derived table: one SELECT, a schedule
        except DerivedError as e:
            raise PipelineAgentError(str(e))
        sql = row["sql"]
        for s, t in self._reads(sql):
            if not can_view(self.derived.urn(s, t)):                    # fail closed: only tables the person may see
                raise PipelineAgentError(f"the query reads {s}.{t}, a table you may not see")
        try:
            self.run(actor, f"EXPLAIN {sql}", 1)
            cols, rows, _ = self.run(actor, f"SELECT * FROM ({sql}) AS candidate LIMIT {PREVIEW}", PREVIEW)
        except Exception as e:  # noqa: BLE001 - Trino's refusal goes back to the model
            raise PipelineAgentError(f"Trino refused the query: {str(e).splitlines()[0][:600]}")
        names = [c if isinstance(c, str) else c.get("name") for c in cols]
        lower = {str(n).lower() for n in names}
        if body["mode"] == "append" and str(body["watermark"]).lower() not in lower:
            raise PipelineAgentError(f"the watermark {body['watermark']} is not a column of the result")
        builtin = []
        if body["mode"] == "merge":
            missing = [k for k in body["key"] if k.lower() not in lower]
            if missing:
                raise PipelineAgentError(f"the key column {missing[0]} is not a column of the result")
            keys = ", ".join(f'"{k}"' for k in body["key"])
            builtin.append({"name": "key_is_unique", "by": "cairn",
                            "sql": f"SELECT count(*) FROM (SELECT {keys} FROM candidate GROUP BY {keys} "
                                   "HAVING count(*) > 1) AS d"})
        model_checks = []
        for c in (plan.get("checks") or [])[:MAX_CHECKS]:
            if not CHECK_NAME.match(str(c.get("name") or "")) or c["name"] in {x["name"] for x in checks + builtin}:
                continue
            try:
                model_checks.append({"name": c["name"], "sql": check_sql(str(c.get("sql") or "")), "by": "agent"})
            except PipelineAgentError as e:
                raise PipelineAgentError(f"check {c['name']}: {e}")
        all_checks = builtin + checks + model_checks
        results = [self._check(actor, f"({sql})", c) for c in all_checks]
        proposal = {**{k: body[k] for k in ("name", "target", "mode", "watermark", "key", "every_minutes", "on_change")},
                    "sql": sql, "checks": all_checks, "explanation": str(plan.get("explanation") or "")[:1000],
                    "columns": names}
        return proposal, results, {"columns": names, "rows": rows}

    def _check(self, actor: str, source: str, c: dict) -> dict:
        try:
            _, rows, _ = self.run(actor, f"WITH candidate AS (SELECT * FROM {source} AS src) {c['sql']}", 1)
            found = rows[0][0] if rows and rows[0] else None
            if isinstance(found, bool) or not isinstance(found, (int, float)):
                raise PipelineAgentError(f"check {c['name']} did not return a count")
        except PipelineAgentError:
            raise
        except Exception as e:  # noqa: BLE001 - a check Trino refuses fails
            raise PipelineAgentError(f"check {c['name']}: Trino refused it: {str(e).splitlines()[0][:400]}")
        return {"name": c["name"], "by": c["by"], "found": found, "passed": found == 0}

    # ---- shipping --------------------------------------------------------------------------------------
    def approve(self, actor: str, tid: str) -> dict:
        from cairn.pipelines.derived import DerivedError

        t = self._load(actor, tid)
        if t["status"] != "ready":
            raise PipelineAgentError(f"only a ready task ships (this one is {t['status']})", 409)
        p = t["proposal"]
        body = {k: p[k] for k in ("name", "target", "sql", "mode", "watermark", "key", "every_minutes", "on_change")}
        outcome: dict[str, Any] = {}
        try:
            if t["kind"] == "fix":
                d = self._owned_derived(actor, t["derived_id"])
                d = self.derived.update(d["id"], {**body, "name": d["name"]}, actor)
                run = self.derived.refresh(d["id"], full=True, trigger="manual")
            else:
                d = self.derived.create(body, actor, actor)
                run = self.derived.refresh(d["id"], trigger="manual")
        except DerivedError as e:
            raise PipelineAgentError(str(e), e.status)
        outcome.update(derived_id=d["id"], run={k: run[k] for k in ("kind", "status", "error")})
        status = "shipped"
        if run["status"] != "ok":
            status = "ship_failed"
        else:
            schema, table = p["target"].split(".")
            src = f'"{self.derived.catalog}"."{schema}"."{table}"'
            try:
                outcome["checks"] = [self._check(actor, src, c) for c in p["checks"]]
            except PipelineAgentError as e:
                outcome["checks"] = [{"error": str(e)}]
            if any(not c.get("passed") for c in outcome["checks"]):
                status = "ship_failed"
        outcome["as_code"] = {"derived": [{k: v for k, v in body.items() if v not in (None, [], "")}]}
        with self.engine.begin() as conn:
            conn.execute(update(agent_tasks).where(agent_tasks.c.id == tid).values(
                status=status, outcome=json.dumps(outcome), derived_id=d["id"], updated_ms=_now()))
        self._audit("pipeline_agent.shipped", actor, tid, {"target": p["target"], "status": status,
                                                            "derived_id": d["id"], "run": run["status"]})
        return self.get(actor, tid)

    def reject(self, actor: str, tid: str) -> dict:
        t = self._load(actor, tid)
        if t["status"] not in ("ready", "failed"):
            raise PipelineAgentError(f"a {t['status']} task cannot be rejected", 409)
        with self.engine.begin() as conn:
            conn.execute(update(agent_tasks).where(agent_tasks.c.id == tid).values(status="rejected", updated_ms=_now()))
        self._audit("pipeline_agent.rejected", actor, tid, {})
        return self.get(actor, tid)

    # ---- reading ---------------------------------------------------------------------------------------
    def _load(self, actor: str, tid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(agent_tasks).where(agent_tasks.c.id == tid)).first()
        if r is None or r.owner != actor:
            raise PipelineAgentError("no such task", 404)
        d = dict(r._mapping)
        for k in ("proposal", "checks", "steps", "outcome"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def get(self, actor: str, tid: str) -> dict:
        return self._load(actor, tid)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(agent_tasks.c.id, agent_tasks.c.kind, agent_tasks.c.target, agent_tasks.c.status,
                                       agent_tasks.c.created_ms).where(agent_tasks.c.owner == actor)
                                .order_by(agent_tasks.c.created_ms.desc()).limit(200)).all()
        return [dict(r._mapping) for r in rows]

    def _audit(self, action: str, actor: str, tid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("lakehouse", action, actor, f"pipeline_agent:{tid}", details)


def build_pipeline_agent_router(a: PipelineAgent, current_principal, can_view) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/pipeline-agent/tasks", tags=["lakehouse"])

    def person(p) -> str:
        if p.is_service:
            raise HTTPException(403, "the pipeline agent works for people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except PipelineAgentError as e:
            raise HTTPException(e.status, str(e))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: a.create(person(p), body, can_view(p)))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: a.list(person(p)))

    @router.get("/{tid}")
    async def get(tid: str, p=Depends(current_principal)):
        return await call(lambda: a.get(person(p), tid))

    @router.post("/{tid}/approve")
    async def approve(tid: str, p=Depends(current_principal)):
        return await call(lambda: a.approve(person(p), tid))

    @router.post("/{tid}/reject")
    async def reject(tid: str, p=Depends(current_principal)):
        return await call(lambda: a.reject(person(p), tid))

    return router
