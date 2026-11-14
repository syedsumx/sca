"""Cairn Flows and Airflow, both ways.

**Deploying** (``runner: airflow``): on each publish Cairn writes the flow's DAG (:mod:`cairn.flows.airflow_dag`,
every step delegated to Cairn as the owner) as ``cairn__<flow>.py`` into Airflow's DAG folder, which is either a
mounted volume (``CAIRN_FLOWS_AIRFLOW_DAGS=path:/opt/airflow/dags/cairn``) or a git folder that Airflow's git-sync
reads (``CAIRN_FLOWS_AIRFLOW_DAGS=git:<git folder id>``: committed onto the folder's branch). Deleting the flow
removes the file.

**Reading Airflow** (``GET /v1/airflow/dags``): Airflow's REST API through Cairn's service account
(``CAIRN_AIRFLOW_URL`` and its credentials). Airflow cannot be asked as the person (its sign-in through Cairn is a
browser flow), so Cairn shows each person only the DAGs that are theirs: a DAG one of whose ``owners`` is the
person's user name or one of their groups, or tagged ``team:<group>`` or ``group:<group>`` with one of their groups,
or tagged ``cairn-flow:<flow>`` for a flow they may see; admins see every DAG. When Cairn cannot tell (their groups
cannot be read), it shows only what their user name gives (fail closed); Airflow unreachable is an error, never an
empty list that looks like "no DAGs".

**Converting** a DAG to a draft flow reads the DAG's source (``GET /dagSources``) for the tasks' arguments and its
tasks (``GET /dags/{id}/tasks``) for the order: every task must be an operator of ``cairn-airflow-provider`` (Trino,
check, dbt, Spark), a ``TriggerDagRunOperator`` or an ``EmptyOperator`` (dropped, its edges kept), with literal
arguments; otherwise the answer names each task that blocks and why.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import tempfile
from typing import Any, Callable, Optional
from urllib.parse import quote

from cairn.flows import model
from cairn.flows.model import FlowError
from cairn.flows.steps import Airflow, StepFailed

logger = logging.getLogger(__name__)

# Airflow task instance states as a flow step's.
TI_STATUS = {"success": "ok", "failed": "failed", "upstream_failed": "skipped", "skipped": "skipped",
             "removed": "cancelled", "up_for_retry": "retrying", "up_for_reschedule": "waiting", "deferred": "waiting",
             "queued": "running", "scheduled": "pending", "running": "running", "restarting": "running",
             None: "pending", "none": "pending"}
RULES = {"all_success": "all_ok", "none_failed": "none_failed", "all_done": "all_done", "one_failed": "any_failed",
         "none_failed_min_one_success": "none_failed"}
PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
PARAM_TYPES = {"string": "string", "integer": "int", "number": "float", "boolean": "bool", "array": "list"}
JINJA = re.compile(r"\{\{\s*(.+?)\s*\}\}|\{%")


def airflow_or_error(cfg: dict) -> Airflow:
    try:
        return Airflow(cfg or {})
    except StepFailed as e:
        raise FlowError(str(e), 503)


def newest_first(af: Airflow) -> str:
    """The order of a DAG's runs, newest first: Airflow 3 orders by ``run_after``, Airflow 2 by ``execution_date``."""
    return "-execution_date" if af.cfg.get("api") == "v1" else "-run_after"


def call(af: Airflow, method: str, path: str, body: Any = None) -> Any:
    try:
        return af.call(method, path, body)
    except StepFailed as e:
        msg = str(e)
        status = 404 if "said 404" in msg else 502
        raise FlowError(msg if status == 404 else f"Airflow: {msg}", status)


# ---- deploying a flow's DAG --------------------------------------------------------------------------------
class DagFolder:
    """Where Cairn writes the DAGs of ``runner: airflow`` flows: a directory, or a git folder."""

    def __init__(self, spec: str, git_folders: Any = None):
        self.spec = spec.strip()
        self.git_folders = git_folders
        kind, _, where = self.spec.partition(":")
        if kind not in ("path", "git") or not where:
            raise ValueError("CAIRN_FLOWS_AIRFLOW_DAGS is path:<directory> or git:<git folder id>")
        self.kind, self.where = kind, where

    @classmethod
    def from_env(cls, git_folders: Any = None) -> Optional["DagFolder"]:
        spec = os.environ.get("CAIRN_FLOWS_AIRFLOW_DAGS", "").strip()
        return cls(spec, git_folders) if spec else None

    @staticmethod
    def file_name(flow: str) -> str:
        return f"cairn__{flow}.py"

    def describe(self) -> str:
        return f"{self.kind}:{self.where}"

    def write(self, flow: str, text: Optional[str], message: str) -> dict:
        """Writes (or with ``text`` None, removes) the flow's DAG file; raises FlowError when it cannot."""
        name = self.file_name(flow)
        if self.kind == "path":
            try:
                os.makedirs(self.where, exist_ok=True)
                target = os.path.join(self.where, name)
                if text is None:
                    if os.path.exists(target):
                        os.remove(target)
                    return {"target": self.describe(), "file": name, "removed": True}
                fd, tmp = tempfile.mkstemp(prefix=".cairn-", suffix=".tmp", dir=self.where)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.chmod(tmp, 0o644)
                os.replace(tmp, target)            # Airflow never reads half a file
            except OSError as e:
                raise FlowError(f"cannot write the DAG to {self.where}: {e.strerror or e}", 502)
            return {"target": self.describe(), "file": name}
        if self.git_folders is None:
            raise FlowError("git folders are not set up here, so CAIRN_FLOWS_AIRFLOW_DAGS=git:... cannot be used", 503)
        from cairn.sqlworkspace.workspace import WorkspaceError

        try:
            out = self.git_folders.commit_files(None, self.where, {name: text}, message)
        except WorkspaceError as e:
            raise FlowError(f"cannot commit the DAG to the git folder: {e}", 502)
        return {"target": self.describe(), "file": name, "commit": out.get("commit")}


# ---- Airflow's DAGs, as Cairn shows them ---------------------------------------------------------------------
def _names(xs: Any) -> list[str]:
    out = []
    for x in xs or []:
        out.append(str(x.get("name")) if isinstance(x, dict) else str(x))
    return out


class AirflowDags:
    def __init__(self, airflow_cfg: dict, groups_of: Callable[[str], list[str]], is_admin: Callable[[str], bool],
                 flows: Any = None, sql_connection: Optional[str] = None):
        self.cfg = airflow_cfg
        self.groups_of, self.is_admin, self.flows = groups_of, is_admin, flows
        self.sql_connection = sql_connection or os.environ.get("CAIRN_FLOWS_AIRFLOW_SQL_CONNECTION", "lakehouse")

    def _af(self) -> Airflow:
        return airflow_or_error(self.cfg)

    # ---- who sees what ------------------------------------------------------------------------------------
    def _groups(self, actor: str) -> set[str]:
        try:
            return {str(g).removeprefix("urn:li:corpGroup:") for g in (self.groups_of(actor) or [])}
        except Exception:  # noqa: BLE001 - unknown groups: only the person's own name counts (fail closed)
            logger.warning("airflow: could not resolve the groups of %s", actor)
            return set()

    def _admin(self, actor: str) -> bool:
        try:
            return bool(self.is_admin(actor))
        except Exception:  # noqa: BLE001 - cannot tell: not an admin
            return False

    def visible(self, actor: str, dag: dict, groups: Optional[set[str]] = None, admin: Optional[bool] = None) -> bool:
        if admin if admin is not None else self._admin(actor):
            return True
        user = actor.removeprefix("urn:li:corpuser:")
        groups = self._groups(actor) if groups is None else groups
        owners = {str(o) for o in dag.get("owners") or []}
        if user in owners or owners & groups:
            return True
        for t in _names(dag.get("tags")):
            kind, _, value = t.partition(":")
            if kind in ("team", "group") and value in groups:
                return True
            if kind == "cairn-flow" and self.flows is not None:
                try:
                    if self.flows._role(actor, self.flows._row(value)) is not None:
                        return True
                except FlowError:
                    pass
        return False

    def _all(self, af: Airflow) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = call(af, "GET", f"/dags?limit=100&offset={offset}&order_by=dag_id")
            dags = page.get("dags") or []
            out += dags
            offset += len(dags)
            if not dags or offset >= int(page.get("total_entries") or 0) or offset >= 5000:
                return out

    def list(self, actor: str) -> list[dict]:
        af = self._af()
        groups, admin = self._groups(actor), self._admin(actor)
        mine = [d for d in self._all(af) if self.visible(actor, d, groups, admin)]
        out = []
        for d in mine[:500]:
            last = None
            try:
                runs = call(af, "GET", f"/dags/{quote(d['dag_id'], safe='')}/dagRuns?limit=1&order_by={newest_first(af)}")
                last = (runs.get("dag_runs") or [None])[0]
            except FlowError:
                pass
            out.append({"dag_id": d["dag_id"], "description": d.get("description") or "", "owners": d.get("owners") or [],
                        "tags": _names(d.get("tags")), "is_paused": bool(d.get("is_paused")),
                        "schedule": schedule_of(d) or None, "schedule_text": d.get("timetable_description"),
                        "has_import_errors": bool(d.get("has_import_errors")),
                        "cairn_flow": next((t.split(":", 1)[1] for t in _names(d.get("tags"))
                                            if t.startswith("cairn-flow:")), None),
                        "last_run": {"dag_run_id": last["dag_run_id"], "state": last.get("state"),
                                     "start_date": last.get("start_date"), "end_date": last.get("end_date"),
                                     "run_type": last.get("run_type")} if last else None})
        return out

    def _dag(self, af: Airflow, actor: str, dag_id: str) -> dict:
        try:
            dag = call(af, "GET", f"/dags/{quote(dag_id, safe='')}/details")
            if af.cfg.get("api") == "v1":            # Airflow 2's details leave owners and tags empty: the DAG has them
                listed = call(af, "GET", f"/dags/{quote(dag_id, safe='')}")
                dag = {**dag, "owners": listed.get("owners") or [], "tags": listed.get("tags") or []}
        except FlowError as e:
            if e.status == 404:
                raise FlowError("no such DAG", 404)
            raise
        if not self.visible(actor, dag):
            raise FlowError("no such DAG", 404)               # not theirs: as if it were not there
        return dag

    def get(self, actor: str, dag_id: str, run: Optional[str] = None) -> dict:
        af = self._af()
        dag = self._dag(af, actor, dag_id)
        q = quote(dag_id, safe="")
        tasks = call(af, "GET", f"/dags/{q}/tasks").get("tasks") or []
        runs = call(af, "GET", f"/dags/{q}/dagRuns?limit=10&order_by={newest_first(af)}").get("dag_runs") or []
        chosen = next((r for r in runs if r["dag_run_id"] == run), None) if run else (runs[0] if runs else None)
        if run and chosen is None:
            try:
                chosen = call(af, "GET", f"/dags/{q}/dagRuns/{quote(run, safe='')}")
            except FlowError:
                raise FlowError("no such run of that DAG", 404)
        states: dict[str, Any] = {}
        tries: dict[str, int] = {}
        if chosen is not None:
            tis = call(af, "GET", f"/dags/{q}/dagRuns/{quote(chosen['dag_run_id'], safe='')}/taskInstances?limit=1000")
            for ti in tis.get("task_instances") or []:
                if ti.get("map_index", -1) in (-1, 0):
                    states[ti["task_id"]] = ti.get("state")
                    tries[ti["task_id"]] = ti.get("try_number") or 0
        up: dict[str, list[str]] = {t["task_id"]: [] for t in tasks}
        for t in tasks:
            for d in t.get("downstream_task_ids") or []:
                up.setdefault(d, []).append(t["task_id"])
        steps = []
        for t in tasks:
            cls = (t.get("class_ref") or {}).get("class_name") or t.get("operator_name") or "Operator"
            steps.append({"name": t["task_id"], "type": _canvas_type(cls), "after": sorted(up.get(t["task_id"], [])),
                          "operator": cls, "description": f"{cls}" + (f": {t['doc_md']}" if t.get("doc_md") else ""),
                          "retries": int(t.get("retries") or 0), "trigger_rule": t.get("trigger_rule")})
        conversion = self._conversion(af, dag, tasks, up)
        return {"dag_id": dag_id, "description": dag.get("description") or "", "owners": dag.get("owners") or [],
                "tags": _names(dag.get("tags")), "is_paused": bool(dag.get("is_paused")),
                "schedule": schedule_of(dag) or None, "schedule_text": dag.get("timetable_description"),
                "timezone": dag.get("timezone"), "file": dag.get("relative_fileloc") or dag.get("fileloc"),
                "steps": steps,
                "runs": [{"dag_run_id": r["dag_run_id"], "state": r.get("state"), "run_type": r.get("run_type"),
                          "start_date": r.get("start_date"), "end_date": r.get("end_date"),
                          "logical_date": r.get("logical_date"), "conf": r.get("conf") or {}} for r in runs],
                "run": chosen["dag_run_id"] if chosen else None, "run_state": chosen.get("state") if chosen else None,
                "states": states, "tries": tries,
                "statuses": {k: TI_STATUS.get(v, "running") for k, v in states.items()},
                "conversion": conversion}

    # ---- converting ---------------------------------------------------------------------------------------
    def _source(self, af: Airflow, dag: dict) -> Optional[str]:
        """The DAG file's source: by DAG id (Airflow 3), or by the DAG's file token (Airflow 2)."""
        try:
            if af.cfg.get("api") == "v1":
                token = dag.get("file_token") or call(af, "GET", f"/dags/{quote(dag['dag_id'], safe='')}").get("file_token")
                return call(af, "GET", f"/dagSources/{quote(str(token), safe='')}").get("content") if token else None
            return call(af, "GET", f"/dagSources/{quote(dag['dag_id'], safe='')}").get("content")
        except FlowError:
            return None

    def _conversion(self, af: Airflow, dag: dict, tasks: list[dict], up: dict[str, list[str]]) -> dict:
        source = self._source(af, dag)
        calls = _calls(source) if source else {}
        blockers: list[dict] = []
        notes: list[str] = []
        if source is None:
            blockers.append({"task": None, "reason": "Airflow does not give this DAG's source, so its tasks' arguments "
                                                     "cannot be read"})
        dropped: set[str] = set()
        steps: dict[str, dict] = {}
        names = _step_names([t["task_id"] for t in tasks])
        for t in tasks:
            tid = t["task_id"]
            cls = (t.get("class_ref") or {}).get("class_name") or t.get("operator_name") or ""
            if t.get("is_mapped"):
                blockers.append({"task": tid, "operator": cls, "reason": "a mapped task (expand): use a for-each step"})
                continue
            if cls == "EmptyOperator":
                dropped.add(tid)
                continue
            kw = calls.get(tid)
            if kw is None and source is not None:
                blockers.append({"task": tid, "operator": cls, "reason": "its call is not found in the DAG file (made in "
                                                                         "a loop or a function)"})
                continue
            try:
                step = _step(cls, kw or {}, self.sql_connection)
            except _Blocked as b:
                blockers.append({"task": tid, "operator": cls, "reason": str(b)})
                continue
            rule = t.get("trigger_rule") or "all_success"
            if rule not in RULES:
                blockers.append({"task": tid, "operator": cls, "reason": f"trigger rule {rule} has no flow equivalent"})
                continue
            if RULES[rule] != "all_ok":
                step["when"] = RULES[rule]
            if t.get("retries"):
                step["retries"] = max(0, min(int(t["retries"]), 10))
                delay = _seconds(t.get("retry_delay"))
                if delay is not None:
                    step["retry_delay_s"] = max(0, min(delay, 3600))
                step["retry_backoff"] = 2 if t.get("retry_exponential_backoff") else 1
            timeout = _seconds(t.get("execution_timeout"))
            if timeout:
                step["timeout_s"] = max(1, min(timeout, 7 * 86400))
            if t.get("doc_md"):
                step["description"] = str(t["doc_md"])[:2000]
            steps[tid] = step

        def parents(tid: str, seen: frozenset = frozenset()) -> list[str]:
            out: list[str] = []
            for p in up.get(tid, []):
                if p in dropped and p not in seen:
                    out += parents(p, seen | {p})
                elif p not in dropped:
                    out.append(p)
            return out
        if blockers:
            return {"convertible": False, "blockers": blockers, "notes": notes, "flow": None}
        flow_steps = []
        for tid, step in steps.items():
            after = sorted({names[p] for p in parents(tid) if p in steps})
            flow_steps.append({"name": names[tid], **({"after": after} if after else {}), **step})
        if not flow_steps:
            return {"convertible": False, "notes": notes, "flow": None,
                    "blockers": [{"task": None, "reason": "the DAG has no tasks that do anything"}]}
        name = _flow_name(dag["dag_id"])
        defn: dict[str, Any] = {"kind": "flow", "name": name,
                                "description": (dag.get("description") or f"Converted from the Airflow DAG {dag['dag_id']}")[:4000],
                                "steps": flow_steps}
        sched = schedule_of(dag).strip()
        cron = PRESETS.get(sched, sched)
        if cron and len(cron.split()) == 5:
            tz = dag.get("timezone") or "UTC"
            defn["triggers"] = [{"type": "schedule", "cron": cron, "timezone": tz if tz != "Timezone('UTC')" else "UTC"}]
        elif sched and sched not in ("None", "null"):
            notes.append(f"its schedule ({dag.get('timetable_description') or sched}) is not a cron schedule: the flow "
                         "starts by hand until you add a trigger")
        params = []
        for pname, p in (dag.get("params") or {}).items():
            if not model.PARAM_NAME.match(pname):
                continue
            schema = (p or {}).get("schema") or {}
            ptype = schema.get("type")
            ptype = next((x for x in ptype if x != "null"), "string") if isinstance(ptype, list) else ptype
            one: dict[str, Any] = {"name": pname, "type": PARAM_TYPES.get(ptype or "string", "string")}
            if schema.get("format") == "date":
                one["type"] = "date"
            if (p or {}).get("value") is not None:
                one["default"] = p["value"]
            if (p or {}).get("description"):
                one["description"] = str(p["description"])[:500]
            params.append(one)
        if params:
            defn["params"] = params
        check = model.validate(defn)
        if not check["valid"]:
            return {"convertible": False, "notes": notes, "flow": None,
                    "blockers": [{"task": e.get("step"), "reason": f"{e.get('path') or 'flow'}: {e['message']}"}
                                 for e in check["errors"]]}
        return {"convertible": True, "blockers": [], "notes": notes, "flow": model.compact(check["flow"])}

    def convert(self, actor: str, dag_id: str, name: Optional[str] = None) -> dict:
        if self.flows is None:
            raise FlowError("flows are not set up here", 503)
        detail = self.get(actor, dag_id)
        conv = detail["conversion"]
        if not conv["convertible"]:
            lines = "; ".join(f"{b['task'] or 'the DAG'}: {b['reason']}" for b in conv["blockers"][:10])
            raise FlowError(f"{dag_id} cannot be converted: {lines}", 409,
                            [{"path": f"tasks.{b['task']}" if b.get("task") else "", "message": b["reason"],
                              **({"step": b["task"]} if b.get("task") else {})} for b in conv["blockers"]])
        defn = dict(conv["flow"])
        if name:
            if not model.NAME.match(name):
                raise FlowError("name: lowercase letters, digits, - and _ (starting with a letter)")
            defn["name"] = name
        made = self.flows.create(actor, defn)
        return {"flow": made["name"], "notes": conv["notes"], "draft": made["draft"]}


class _Blocked(Exception):
    pass


def _canvas_type(cls: str) -> str:
    return {"CairnTrinoOperator": "sql", "CairnCheckOperator": "check", "CairnDbtOperator": "dbt",
            "CairnSparkApplicationOperator": "spark", "TriggerDagRunOperator": "airflow",
            "CairnQualityGateOperator": "check", "CairnQualitySensor": "wait", "BranchPythonOperator": "branch",
            "CairnFlowBranchOperator": "branch", "EmptyOperator": "wait",
            "CairnDbtTaskGroup": "dbt"}.get(cls, "python")


def schedule_of(dag: dict) -> str:
    """A DAG's schedule as text: Airflow 3's ``timetable_summary``, or Airflow 2's ``schedule_interval`` (a cron
    expression or a preset, as ``{"__type": "CronExpression", "value": ...}``)."""
    if dag.get("timetable_summary"):
        return str(dag["timetable_summary"])
    si = dag.get("schedule_interval")
    if isinstance(si, dict):
        return str(si.get("value") or "") if si.get("__type") == "CronExpression" else ""
    return str(si or "")


def _seconds(td: Any) -> Optional[int]:
    if isinstance(td, dict):
        return int(td.get("days", 0)) * 86400 + int(td.get("seconds", 0))
    if isinstance(td, (int, float)):
        return int(td)
    return None


def _flow_name(dag_id: str) -> str:
    n = re.sub(r"[^a-z0-9_-]+", "-", dag_id.lower()).strip("-_")[:63] or "airflow-dag"
    return n if n[0].isalpha() else ("dag-" + n)[:63]


def _step_names(ids: list[str]) -> dict[str, str]:
    out, taken = {}, set()
    for tid in ids:
        n = re.sub(r"[^A-Za-z0-9_-]+", "-", tid).strip("-")[:63] or "task"
        if not n[0].isalpha():
            n = ("t-" + n)[:63]
        base, i = n, 2
        while n in taken:
            n, i = f"{base[:58]}-{i}", i + 1
        taken.add(n)
        out[tid] = n
    return out


def _calls(source: str) -> dict[str, dict]:
    """task_id -> the literal keyword arguments of the call that made it (a value that is not a literal: _UNKNOWN)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kws = {k.arg: k.value for k in node.keywords if k.arg}
        tid = kws.get("task_id")
        if not isinstance(tid, ast.Constant) or not isinstance(tid.value, str):
            continue
        args: dict[str, Any] = {}
        for k, v in kws.items():
            try:
                args[k] = ast.literal_eval(v)
            except (ValueError, SyntaxError, TypeError):
                args[k] = _UNKNOWN
        if any(k.arg is None for k in node.keywords):
            args["**"] = _UNKNOWN
        out[tid.value] = args
    return out


_UNKNOWN = object()
COMMON = {"task_id", "retries", "retry_delay", "retry_exponential_backoff", "execution_timeout", "trigger_rule",
          "doc_md", "doc", "owner", "pool", "queue", "priority_weight", "cairn_conn_id", "token_ttl", "return_rows",
          "dag", "do_xcom_push", "email", "email_on_failure", "email_on_retry", "max_retry_delay", "sla"}


def _need_literals(kw: dict, fields: tuple[str, ...]) -> None:
    for f in fields:
        if kw.get(f) is _UNKNOWN:
            raise _Blocked(f"{f} is computed when the DAG file is read, not written in it")
    if kw.get("**") is _UNKNOWN:
        raise _Blocked("its arguments come from a **mapping")


def _templates_ok(v: Any, field: str) -> None:
    if isinstance(v, str):
        for m in JINJA.finditer(v):
            if m.group(0).startswith("{%") or not re.match(r"^params\.[A-Za-z_][A-Za-z0-9_]*$", (m.group(1) or "").strip()):
                raise _Blocked(f"{field} uses Airflow templates ({m.group(0)[:40]}) that a flow does not have "
                               "(only {{ params.x }} carries over)")
    elif isinstance(v, (list, tuple)):
        for x in v:
            _templates_ok(x, field)
    elif isinstance(v, dict):
        for x in v.values():
            _templates_ok(x, field)


def _only(kw: dict, allowed: set[str], cls: str) -> None:
    extra = sorted(k for k in kw if k not in allowed | COMMON and k != "**")
    if extra:
        raise _Blocked(f"{', '.join(extra)} of {cls} has no flow equivalent")


def _step(cls: str, kw: dict, connection: str) -> dict:
    if cls in ("CairnFlowStepOperator", "CairnFlowBranchOperator"):
        raise _Blocked(f"generated from the Cairn flow {kw.get('flow') if isinstance(kw.get('flow'), str) else ''}: "
                       "open the flow in Cairn instead")
    if cls in ("CairnTrinoOperator", "CairnCheckOperator"):
        _need_literals(kw, ("sql", "schema", "measure", "op", "value"))
        _only(kw, {"sql", "schema", "measure", "op", "value", "template_ext"}, cls)
        if kw.get("schema"):
            raise _Blocked(f"it sets schema={kw['schema']!r}; a flow's SQL names its tables in full")
        sql = kw.get("sql")
        if isinstance(sql, (list, tuple)):
            if len(sql) != 1:
                raise _Blocked("several statements: a flow's SQL step runs one (make one step each)")
            sql = sql[0]
        if not isinstance(sql, str) or not sql.strip():
            raise _Blocked("no SQL")
        if sql.strip().endswith(".sql"):
            raise _Blocked("its SQL is a file the DAG reads; put the SQL itself in the step")
        if ";" in sql.strip().rstrip(";"):
            raise _Blocked("several statements: a flow's SQL step runs one (make one step each)")
        _templates_ok(sql, "sql")
        out: dict[str, Any] = {"type": "sql" if cls == "CairnTrinoOperator" else "check",
                               "sql": sql.strip().rstrip(";"), "connection": connection}
        if cls == "CairnCheckOperator":
            if kw.get("op") not in model.OPS:
                raise _Blocked("op is not one of " + " ".join(model.OPS))
            out.update(measure=str(kw.get("measure", "rows")), op=kw["op"], value=float(kw.get("value", 0)))
        return out
    if cls == "CairnDbtOperator":
        _need_literals(kw, ("project_dir", "command", "select"))
        _only(kw, {"project_dir", "command", "select"}, cls)
        command = kw.get("command", "build")
        if command not in ("run", "build", "test", "seed", "snapshot"):
            raise _Blocked(f"dbt {command}: a flow's dbt step runs run, build, test, seed or snapshot")
        out = {"type": "dbt", "command": command, "project_dir": str(kw.get("project_dir") or "/dbt")}
        if kw.get("select"):
            _templates_ok(kw["select"], "select")
            out["select"] = str(kw["select"])
        return out
    if cls == "CairnSparkApplicationOperator":
        _need_literals(kw, ("main", "arguments", "spark_conf"))
        _only(kw, {"name", "main", "arguments", "spark_conf", "image", "service_account", "cairn_url", "catalog_uri",
                   "namespace", "executors", "executor_cores", "executor_memory", "driver_cores", "driver_memory",
                   "kube_config_file", "poll_interval", "delete_on_success"}, cls)
        out = {"type": "spark", "application": str(kw.get("main") or "")}
        if not out["application"]:
            raise _Blocked("no main application")
        if kw.get("arguments"):
            _templates_ok(kw["arguments"], "arguments")
            out["args"] = [str(a) for a in kw["arguments"]]
        if kw.get("spark_conf"):
            out["conf"] = dict(kw["spark_conf"])
        return out
    if cls == "TriggerDagRunOperator":
        _need_literals(kw, ("trigger_dag_id", "conf", "wait_for_completion", "poke_interval"))
        _only(kw, {"trigger_dag_id", "conf", "wait_for_completion", "poke_interval", "deferrable"}, cls)
        if not isinstance(kw.get("trigger_dag_id"), str):
            raise _Blocked("no trigger_dag_id")
        _templates_ok(kw.get("conf") or {}, "conf")
        out = {"type": "airflow", "dag_id": kw["trigger_dag_id"], "wait": bool(kw.get("wait_for_completion", False))}
        if kw.get("conf"):
            out["conf"] = dict(kw["conf"])
        if kw.get("poke_interval") is not None:
            out["poll_s"] = max(5, min(int(kw["poke_interval"]), 600))
        return out
    if cls == "CairnQualityGateOperator":
        raise _Blocked("a quality gate on Cairn's quality status has no flow step yet; use a check step")
    raise _Blocked(f"{cls or 'this operator'} has no flow step: only cairn-airflow-provider's operators (Trino, check, "
                   "dbt, Spark), TriggerDagRunOperator and EmptyOperator convert")
