"""Agent trace intelligence: what each agent's recorded tool calls and model use say about how it behaves (L101).

Built on the tool-call trace (:mod:`cairn.ai.traces`, ``cairn_agent_calls``) and AI usage metering
(:mod:`cairn.ai.usage`: tasks ``gateway:<agent id>`` for agent keys and agent apps, ``agent.<name>`` for custom
agents). For each agent over a window (``days``):

* **runs**: a custom agent's run, an agent app run's short-lived token, an OAuth access token's session; calls
  made with an agent key (no run of their own) are grouped into sessions per person, split at 30 idle minutes;
* **tool calls**, **errors**, **refusals** (refused or forbidden), **error rate**, latency (average, p95) and
  **slow calls** (at or over ``slow_call_ms``);
* **loops**: the same tool with the same (redacted) arguments repeated within one run at least ``loop_threshold``
  times, the shape of an agent stuck retrying;
* **model use**: calls, tokens and estimated cost;
* **outliers against the agent's own baseline**: the period before the window (up to three windows, within the
  30 days traces are kept): calls per day, error rate, p95 latency, model cost per day and calls per run, each
  compared with the agent's own history (daily mean and spread), never with other agents.

Each signal becomes a **finding** with a severity (``info``, ``warning``, ``critical``). Administrators
(``MANAGE_GLOBAL_SETTINGS``) and operators (``MANAGE_SYSTEM_OPERATIONS``) see every agent; anyone else sees the
agents they own (as in the agent hub). Agents themselves never read it. Reading changes nothing; changing the
thresholds is for administrators and audited (``agent_traces.settings_changed``).

HTTP: ``GET /v1/agent-traces/insights?days=7``, ``GET /v1/agent-traces/insights/{agent_id}?days=7``,
``GET|PUT /v1/agent-traces/settings``.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Callable, Optional

from sqlalchemy import select, update

DAY_MS = 86_400_000
SESSION_GAP_MS = 30 * 60_000
MAX_ROWS = 200_000
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
DEFAULTS = {"loop_threshold": 4, "slow_call_ms": 10_000, "outlier_factor": 2.0, "min_calls": 10,
            "error_rate_warning": 0.2, "error_rate_critical": 0.5}
LIMITS = {"loop_threshold": (2, 1000), "slow_call_ms": (1, 3_600_000), "outlier_factor": (1.1, 100.0),
          "min_calls": (1, 100_000), "error_rate_warning": (0.01, 1.0), "error_rate_critical": (0.01, 1.0)}
SETTINGS_KEY = "trace_insights"


class InsightsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return int(s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)])


def _mean_std(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    m = sum(xs) / len(xs)
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _fid(*parts: Any) -> str:
    return "f_" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


class TraceInsights:
    def __init__(self, engine, agents, may: Callable[[str, str], bool], audit=None, clock=time.time):
        """``agents``: the agent hub (``AgentService``); ``may(actor, privilege)``: the decision point."""
        self.engine, self.agents, self.may, self.audit, self.clock = engine, agents, may, audit, clock

    # ---- settings --------------------------------------------------------------------------------------
    def settings(self) -> dict:
        from cairn.identity.agents import agent_settings

        with self.engine.connect() as conn:
            v = conn.execute(select(agent_settings.c.value).where(agent_settings.c.key == SETTINGS_KEY)).scalar()
        return {**DEFAULTS, **{k: v[k] for k in DEFAULTS if isinstance(v, dict) and k in v}}

    def set_settings(self, actor: str, body: dict) -> dict:
        from cairn.identity.agents import agent_settings

        if not self.may(actor, "MANAGE_GLOBAL_SETTINGS"):
            raise InsightsError("changing trace analysis settings needs MANAGE_GLOBAL_SETTINGS", 403)
        unknown = sorted(set(body) - set(DEFAULTS))
        if unknown:
            raise InsightsError(f"unknown settings {unknown}; settings are {sorted(DEFAULTS)}", 422)
        cur = self.settings()
        for k, v in body.items():
            lo, hi = LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise InsightsError(f"{k}: a number from {lo} to {hi}", 422)
            cur[k] = int(v) if isinstance(DEFAULTS[k], int) else float(v)
        if cur["error_rate_critical"] < cur["error_rate_warning"]:
            raise InsightsError("error_rate_critical must be at least error_rate_warning", 422)
        with self.engine.begin() as conn:
            q = update(agent_settings).where(agent_settings.c.key == SETTINGS_KEY)
            if conn.execute(q.values(value=cur)).rowcount == 0:
                conn.execute(agent_settings.insert().values(key=SETTINGS_KEY, value=cur))
            if self.audit is not None:
                self.audit.record("agents", "agent_traces.settings_changed", actor, "agent-traces",
                                  {k: body[k] for k in sorted(body)}, conn=conn)
        return cur

    # ---- who sees what ---------------------------------------------------------------------------------
    def visible(self, actor: str) -> list[dict]:
        """The agents ``actor`` may analyse: every one for administrators and operators, else their own."""
        everyone = self.agents.list()
        if self.may(actor, "MANAGE_GLOBAL_SETTINGS") or self.may(actor, "MANAGE_SYSTEM_OPERATIONS"):
            return everyone
        return [a for a in everyone if a.get("owner") == actor]

    # ---- the data --------------------------------------------------------------------------------------
    def _now(self) -> int:
        return int(self.clock() * 1000)

    @staticmethod
    def _window(days: int) -> tuple[int, int]:
        from cairn.ai.traces import KEEP_CALLS_DAYS

        if not isinstance(days, int) or not 1 <= days <= KEEP_CALLS_DAYS:
            raise InsightsError(f"days: 1 to {KEEP_CALLS_DAYS}", 422)
        return days, max(0, min(3 * days, KEEP_CALLS_DAYS - days))

    def _calls(self, agent_ids: list[str], since: int, until: int) -> tuple[list, bool]:
        from cairn.ai.traces import agent_calls as t

        if not agent_ids:
            return [], False
        q = (select(t.c.agent, t.c.actor, t.c.run, t.c.tool, t.c.args, t.c.status, t.c.duration_ms, t.c.flagged,
                    t.c.created_ms)
             .where(t.c.agent.in_(agent_ids), t.c.created_ms >= since, t.c.created_ms < until)
             .order_by(t.c.created_ms.desc()).limit(MAX_ROWS + 1))
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return list(reversed(rows[:MAX_ROWS])), len(rows) > MAX_ROWS

    def _model(self, agents: list[dict], since: int, until: int) -> dict[str, list]:
        """Model usage rows per agent id: (ts_ms, input, output, cost)."""
        from cairn.ai.custom_agents import custom_agents
        from cairn.ai.usage import usage_table as u

        if not agents:
            return {}
        ids = {a["id"] for a in agents}
        with self.engine.connect() as conn:
            built = {f"agent.{r.name}"[:64]: r.agent_id for r in conn.execute(
                select(custom_agents.c.name, custom_agents.c.agent_id).where(custom_agents.c.agent_id.in_(ids)))}
            tasks = {f"gateway:{i}"[:64]: i for i in ids} | built
            rows = conn.execute(select(u.c.task, u.c.ts_ms, u.c.input_tokens, u.c.output_tokens, u.c.cost_usd)
                                .where(u.c.ts_ms >= since, u.c.ts_ms < until,
                                       u.c.task.in_(list(tasks))).limit(MAX_ROWS)).all()
        out: dict[str, list] = {}
        for task, ts, i, o, cost in rows:
            out.setdefault(tasks[task], []).append((int(ts), int(i or 0), int(o or 0), float(cost or 0)))
        return out

    @staticmethod
    def _runs(rows: list) -> list[tuple[str, Any]]:
        """Each call with its run: the recorded one, else a session per person split at 30 idle minutes."""
        last: dict[tuple, tuple[int, str]] = {}
        out = []
        for r in rows:
            if r.run:
                out.append((r.run, r))
                continue
            key = (r.agent, r.actor)
            prev = last.get(key)
            if prev is None or r.created_ms - prev[0] > SESSION_GAP_MS:
                sid = "ses_" + hashlib.sha1(f"{r.agent}|{r.actor}|{r.created_ms}".encode()).hexdigest()[:12]
            else:
                sid = prev[1]
            last[key] = (r.created_ms, sid)
            out.append((sid, r))
        return out

    # ---- analysis --------------------------------------------------------------------------------------
    def _analyse(self, agent: dict, rows: list, model: list, start: int, end: int, base_start: int,
                 cfg: dict, detail: bool) -> dict:
        days = max(1, (end - start) // DAY_MS)
        tagged = self._runs(rows)
        cur = [(run, r) for run, r in tagged if r.created_ms >= start]
        base = [(run, r) for run, r in tagged if r.created_ms < start]
        calls = [r for _, r in cur]
        n = len(calls)
        errors = sum(1 for r in calls if r.status == "error")
        refused = sum(1 for r in calls if r.status in ("refused", "forbidden"))
        durations = [int(r.duration_ms) for r in calls]
        slow = [r for r in calls if r.duration_ms >= cfg["slow_call_ms"]]
        runs: dict[str, dict] = {}
        repeats: dict[tuple, int] = {}
        for run, r in cur:
            x = runs.setdefault(run, {"run": run, "actor": r.actor, "calls": 0, "errors": 0, "refused": 0,
                                      "started_ms": r.created_ms, "ended_ms": r.created_ms, "loops": 0})
            x["calls"] += 1
            x["errors"] += r.status == "error"
            x["refused"] += r.status in ("refused", "forbidden")
            x["ended_ms"] = r.created_ms
            repeats[(run, r.tool, r.args)] = repeats.get((run, r.tool, r.args), 0) + 1
        loops = sorted(({"run": run, "tool": tool, "args": args, "count": c}
                        for (run, tool, args), c in repeats.items() if c >= cfg["loop_threshold"]),
                       key=lambda x: -x["count"])
        for lp in loops:
            runs[lp["run"]]["loops"] += 1
        cur_model = [m for m in model if m[0] >= start]
        base_model = [m for m in model if m[0] < start]
        cost = sum(m[3] for m in cur_model)
        summary = {
            "agent": agent["id"], "name": agent["name"], "kind": agent["kind"], "owner": agent.get("owner"),
            "enabled": agent.get("enabled"), "runs": len(runs), "tool_calls": n, "errors": errors, "refused": refused,
            "error_rate": round(errors / n, 4) if n else 0.0, "refusal_rate": round(refused / n, 4) if n else 0.0,
            "flagged": sum(int(r.flagged or 0) for r in calls), "people": len({r.actor for r in calls}),
            "avg_ms": round(sum(durations) / n, 1) if n else 0.0, "p95_ms": _p95(durations), "slow_calls": len(slow),
            "loops": len(loops), "model": {"calls": len(cur_model), "input_tokens": sum(m[1] for m in cur_model),
                                           "output_tokens": sum(m[2] for m in cur_model), "cost_usd": round(cost, 6)},
            "cost_per_run_usd": round(cost / len(runs), 6) if runs else 0.0,
            "last_ms": calls[-1].created_ms if calls else None}
        # The agent's own baseline: the period before the window, per day.
        base_days = max(0, (start - base_start) // DAY_MS)
        baseline = None
        b_calls = [r for _, r in base]
        if base_days and (len(b_calls) >= cfg["min_calls"] or base_model):
            per_day = [0] * base_days
            cost_day = [0.0] * base_days
            for r in b_calls:
                per_day[min(base_days - 1, (r.created_ms - base_start) // DAY_MS)] += 1
            for m in base_model:
                cost_day[min(base_days - 1, (m[0] - base_start) // DAY_MS)] += m[3]
            b_err = sum(1 for r in b_calls if r.status == "error")
            b_runs = len({run for run, _ in base})
            baseline = {"days": base_days, "tool_calls": len(b_calls), "calls_per_day": _mean_std(per_day),
                        "error_rate": b_err / len(b_calls) if b_calls else 0.0,
                        "p95_ms": _p95([int(r.duration_ms) for r in b_calls]),
                        "cost_per_day": _mean_std(cost_day), "calls_per_run": len(b_calls) / b_runs if b_runs else 0.0}
        summary["baseline"] = None if baseline is None else {
            "days": baseline["days"], "tool_calls": baseline["tool_calls"],
            "calls_per_day": round(baseline["calls_per_day"][0], 2), "error_rate": round(baseline["error_rate"], 4),
            "p95_ms": baseline["p95_ms"], "cost_per_day_usd": round(baseline["cost_per_day"][0], 6),
            "calls_per_run": round(baseline["calls_per_run"], 2)}
        findings = self._findings(agent, summary, loops, slow, baseline, days, cfg, n)
        out = {**summary, "findings": findings}
        if detail:
            tools: dict[str, dict] = {}
            for r in calls:
                t = tools.setdefault(r.tool, {"tool": r.tool, "calls": 0, "errors": 0, "refused": 0, "durations": []})
                t["calls"] += 1
                t["errors"] += r.status == "error"
                t["refused"] += r.status in ("refused", "forbidden")
                t["durations"].append(int(r.duration_ms))
            out["tools"] = sorted(({"tool": t["tool"], "calls": t["calls"], "errors": t["errors"],
                                    "refused": t["refused"], "p95_ms": _p95(t["durations"]),
                                    "avg_ms": round(sum(t["durations"]) / t["calls"], 1)} for t in tools.values()),
                                  key=lambda t: -t["calls"])
            out["runs_detail"] = sorted(runs.values(), key=lambda x: (-x["loops"], -x["errors"], -x["calls"]))[:50]
            out["loops_detail"] = loops[:50]
            daily: dict[int, dict] = {}
            for d in range(days):
                daily[d] = {"day": time.strftime("%Y-%m-%d", time.gmtime((start + d * DAY_MS) / 1000)),
                            "tool_calls": 0, "errors": 0, "cost_usd": 0.0}
            for r in calls:
                daily[min(days - 1, (r.created_ms - start) // DAY_MS)]["tool_calls"] += 1
                daily[min(days - 1, (r.created_ms - start) // DAY_MS)]["errors"] += r.status == "error"
            for m in cur_model:
                daily[min(days - 1, (m[0] - start) // DAY_MS)]["cost_usd"] += m[3]
            out["daily"] = [{**v, "cost_usd": round(v["cost_usd"], 6)} for v in daily.values()]
        return out

    @staticmethod
    def _findings(agent: dict, s: dict, loops: list, slow: list, baseline: Optional[dict], days: int, cfg: dict,
                  n: int) -> list[dict]:
        out: list[dict] = []

        def add(kind: str, severity: str, title: str, detail: str, value: Any = None, base: Any = None, **extra):
            out.append({"id": _fid(agent["id"], kind, extra.get("run"), extra.get("tool")), "agent": agent["id"],
                        "agent_name": agent["name"], "kind": kind, "severity": severity, "title": title,
                        "detail": detail, "value": value, "baseline": base, **extra})

        for lp in loops:
            add("loop", "critical" if lp["count"] >= 2 * cfg["loop_threshold"] else "warning",
                f"{lp['tool']} repeated {lp['count']} times in one run",
                f"The agent called {lp['tool']} with the same arguments {lp['count']} times in run {lp['run']} "
                f"(threshold {cfg['loop_threshold']}): it may be stuck retrying.", lp["count"], cfg["loop_threshold"],
                run=lp["run"], tool=lp["tool"])
        if n >= cfg["min_calls"] and s["error_rate"] >= cfg["error_rate_warning"]:
            add("error_rate", "critical" if s["error_rate"] >= cfg["error_rate_critical"] else "warning",
                f"{round(100 * s['error_rate'])}% of tool calls failed",
                f"{s['errors']} of {n} tool calls ended in an error.", s["error_rate"], cfg["error_rate_warning"])
        if n >= cfg["min_calls"] and s["refusal_rate"] >= 0.5:
            add("refusals", "warning", f"{round(100 * s['refusal_rate'])}% of tool calls were refused",
                f"{s['refused']} of {n} tool calls were refused or forbidden: the agent keeps asking for what its "
                "scopes or the person's permissions do not allow.", s["refusal_rate"], 0.5)
        if slow:
            worst = max(slow, key=lambda r: r.duration_ms)
            share = len(slow) / n
            add("slow_calls", "warning" if share >= 0.25 else "info",
                f"{len(slow)} slow tool call{'s' if len(slow) != 1 else ''}",
                f"{len(slow)} of {n} calls took {cfg['slow_call_ms']} ms or more; the slowest was {worst.tool} "
                f"({worst.duration_ms} ms).", len(slow), cfg["slow_call_ms"], tool=worst.tool)
        if s["flagged"]:
            add("injection_flagged", "warning", "Instruction-like catalog text reached the agent",
                f"{s['flagged']} tool results carried text flagged as possible prompt injection.", s["flagged"], 0)
        if baseline is None:
            if n:
                add("no_baseline", "info", "No baseline yet",
                    "The agent has too little history before this window to compare against.", n, None)
            return sorted(out, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["kind"]))
        factor = cfg["outlier_factor"]

        def outlier(kind: str, label: str, value: float, mean: float, std: float, floor: float, unit: str) -> None:
            # Over the agent's own history: at least ``factor`` times its mean and three spreads above it.
            if mean <= 0 or value < factor * mean or value - mean < max(3 * std, floor):
                return
            ratio = value / mean
            add(kind, "critical" if ratio >= 2 * factor else "warning", f"{label} {ratio:.1f}x its baseline",
                f"{label} was {value:g}{unit} against a baseline of {mean:.4g}{unit} "
                f"(over the {baseline['days']} days before).", round(value, 6), round(mean, 6))

        m, sd = baseline["calls_per_day"]
        outlier("volume_outlier", "Tool calls per day", round(n / days, 2), m, sd, 1.0, "")
        m, sd = baseline["cost_per_day"]
        outlier("cost_outlier", "Model cost per day", round(s["model"]["cost_usd"] / days, 6), m, sd, 0.0001, " USD")
        if n >= cfg["min_calls"]:
            outlier("latency_outlier", "p95 tool latency", s["p95_ms"], baseline["p95_ms"], 0, 50, " ms")
            if s["runs"]:
                outlier("calls_per_run_outlier", "Tool calls per run", round(n / s["runs"], 2),
                        baseline["calls_per_run"], 0, 1.0, "")
            b = baseline["error_rate"]
            if s["error_rate"] - b >= 0.1 and s["error_rate"] >= factor * max(b, 0.01):
                add("error_outlier", "critical" if s["error_rate"] >= cfg["error_rate_critical"] else "warning",
                    "Error rate above its baseline",
                    f"{round(100 * s['error_rate'])}% of tool calls failed against {round(100 * b, 1)}% over the "
                    f"{baseline['days']} days before.", s["error_rate"], round(b, 4))
        return sorted(out, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["kind"]))

    # ---- entry points ----------------------------------------------------------------------------------
    def insights(self, actor: str, days: int = 7) -> dict:
        days, base_days = self._window(days)
        cfg = self.settings()
        agents = self.visible(actor)
        end = self._now()
        start = end - days * DAY_MS
        base_start = start - base_days * DAY_MS
        rows, truncated = self._calls([a["id"] for a in agents], base_start, end)
        model = self._model(agents, base_start, end)
        by_agent: dict[str, list] = {}
        for r in rows:
            by_agent.setdefault(r.agent, []).append(r)
        out = []
        for a in agents:
            if a["id"] not in by_agent and a["id"] not in model:
                continue                                    # idle in the window and before: nothing to say
            out.append(self._analyse(a, by_agent.get(a["id"], []), model.get(a["id"], []), start, end, base_start,
                                     cfg, False))
        out = [a for a in out if a["tool_calls"] or a["model"]["calls"] or a["findings"]]
        findings = sorted((f for a in out for f in a.pop("findings")),
                          key=lambda f: (SEVERITY_ORDER[f["severity"]], f["agent_name"], f["kind"]))
        out.sort(key=lambda a: (-a["tool_calls"], a["name"]))
        return {"days": days, "since_ms": start, "until_ms": end, "baseline_days": base_days, "settings": cfg,
                "truncated": truncated, "agents": out, "findings": findings,
                "totals": {"agents": len(out), "runs": sum(a["runs"] for a in out),
                           "tool_calls": sum(a["tool_calls"] for a in out),
                           "errors": sum(a["errors"] for a in out),
                           "cost_usd": round(sum(a["model"]["cost_usd"] for a in out), 6),
                           "critical": sum(1 for f in findings if f["severity"] == "critical"),
                           "warning": sum(1 for f in findings if f["severity"] == "warning")}}

    def agent(self, actor: str, agent_id: str, days: int = 7) -> dict:
        days, base_days = self._window(days)
        a = next((x for x in self.visible(actor) if x["id"] == agent_id), None)
        if a is None:
            raise InsightsError("no such agent", 404)
        cfg = self.settings()
        end = self._now()
        start = end - days * DAY_MS
        base_start = start - base_days * DAY_MS
        rows, truncated = self._calls([agent_id], base_start, end)
        out = self._analyse(a, rows, self._model([a], base_start, end).get(agent_id, []), start, end, base_start,
                            cfg, True)
        return {**out, "days": days, "since_ms": start, "until_ms": end, "baseline_days": base_days,
                "truncated": truncated}


def build_trace_insights_router(ti: TraceInsights, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/agent-traces", tags=["agents"])

    def person(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "agent trace analysis is for people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except InsightsError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/insights")
    async def insights(days: int = 7, p=Depends(current_principal)):
        """Per agent over the window: runs, tool calls, errors, loops, slow calls, model cost, and findings."""
        return await call(lambda: ti.insights(person(p), days))

    @router.get("/settings")
    async def get_settings(p=Depends(current_principal)):
        person(p)
        return await call(ti.settings)

    @router.put("/settings")
    async def put_settings(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ti.set_settings(person(p), body))

    @router.get("/insights/{agent_id}")
    async def agent(agent_id: str, days: int = 7, p=Depends(current_principal)):
        """One agent: its summary and findings, per tool, its runs (loops first), loops and a daily series."""
        return await call(lambda: ti.agent(person(p), agent_id, days))

    return router
