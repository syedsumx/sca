"""A flow as an Airflow DAG file (``GET /v1/flows/{name}/airflow``, ``cairn flows export-airflow``).

The file uses the ``cairn-airflow-provider`` operators and signs in with Airflow's ``cairn`` connection, so it holds
no secret. What maps, and how:

* **Order.** Each step is one task with the same id; ``after`` becomes ``>>``; ``when`` becomes the task's trigger rule
  (``all_ok`` -> ``all_success``, ``none_failed`` -> ``none_failed``, ``all_done`` -> ``all_done``, ``any_failed`` ->
  ``one_failed``). Retries, the delay between them, back-off (Airflow doubles the delay) and the step's time limit map
  to ``retries``, ``retry_delay``, ``retry_exponential_backoff`` and ``execution_timeout``.
* **Parameters** become DAG ``params`` (typed ``Param``); a default that is a Cairn template (``{{ run.logical_date }}``)
  is left to Cairn, which fills it in.
* **Triggers.** A schedule becomes a ``CronTriggerTimetable`` in its time zone; table triggers become assets watched by
  ``CairnDatasetChangedTrigger`` (Cairn's change feed); both together, ``AssetOrTimeSchedule``. A trigger on another
  flow has no Airflow equivalent and is said so in the file. ``concurrency.max_active`` is ``max_active_runs``.
* **Native steps** (``operators="native"``, the default for an export): SQL with ``sql`` and a connection on Trino
  (``CairnTrinoOperator``), a check (``CairnCheckOperator``), dbt (``CairnDbtOperator``), Spark with a Python
  application (``CairnSparkApplicationOperator``) and an Airflow DAG (``TriggerDagRunOperator``). They run as the
  ``cairn`` connection's Cairn user, under that user's grants. A step stays native only when its templates are plain
  ``{{ params.x }}`` (which Airflow's Jinja renders the same way) and no other step reads its output.
* **Delegated steps** run in Cairn, as the flow's owner, through ``CairnFlowStepOperator``: everything else
  (approvals, for-each, waits, derived tables, pipes, streams, notebooks, Python, agents, notifications, saved
  queries), with a comment saying so. A branch is ``CairnFlowBranchOperator`` (a ``BaseBranchOperator`` whose condition
  Cairn evaluates): Airflow skips the side it did not choose, as Cairn does.
* ``operators="cairn"`` delegates every step (what a flow with ``runner: airflow`` deploys), so every step runs as the
  owner whatever the Airflow connection's user may do.
* The file Cairn deploys for ``runner: airflow`` imports ``DAG`` and ``Param`` from ``airflow.sdk`` on Airflow 3 and from
  their Airflow 2 modules otherwise, so the same file runs on either (``CAIRN_AIRFLOW_API`` v2 or v1).
"""

from __future__ import annotations

import re
import textwrap
from typing import Any, Optional

from cairn.flows import expr

PROVIDER = "cairn-airflow-provider"
NATIVE_TYPES = ("sql", "check", "dbt", "spark", "airflow")
TRIGGER_RULES = {"all_ok": "all_success", "none_failed": "none_failed", "all_done": "all_done",
                 "any_failed": "one_failed"}
PARAM_TYPES = {"string": "string", "int": "integer", "float": "number", "bool": "boolean", "date": "string",
               "list": "array"}
PARAM_ONLY = re.compile(r"^params\.[A-Za-z_][A-Za-z0-9_]*$")
WHY_DELEGATED = {
    "derived": "refreshing a derived table, as its owner",
    "pipe": "loading a pipe's files",
    "stream": "waiting for a stream to catch up",
    "notebook": "a notebook job in Cairn's job runner",
    "python": "a Python job in Cairn's job runner",
    "agent": "an agent app, as the owner",
    "approval": "an approval: people decide in Cairn (Flows, or the notification)",
    "wait": "a wait, held by Cairn",
    "for_each": "a for-each: Cairn runs each item",
    "notify": "a notification through Cairn's notification hub",
}


def dag_id_for(flow: str, runner: bool = False) -> str:
    return f"cairn__{flow}" if runner else flow


def _py(v: Any) -> str:
    """A Python literal for a JSON value (strings with several lines as triple-quoted strings when that is exact)."""
    if isinstance(v, str):
        if "\n" in v and '"""' not in v and "\\" not in v and not v.endswith('"'):
            return '"""' + v + '"""'
        return repr(v)
    if v is None or isinstance(v, (bool, int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_py(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_py(str(k))}: {_py(x)}" for k, x in v.items()) + "}"
    return repr(str(v))


def _only_params(value: Any) -> bool:
    """Whether every template in a value is ``{{ params.x }}`` (rendered alike by Cairn and by Airflow's Jinja)."""
    if isinstance(value, str):
        return all(PARAM_ONLY.match(m.group(1).strip()) for m in expr.TEMPLATE.finditer(value))
    if isinstance(value, list):
        return all(_only_params(x) for x in value)
    if isinstance(value, dict):
        return all(_only_params(x) for x in value.values())
    return True


def _read_by_others(steps: list[dict]) -> set[str]:
    """The steps whose outputs other steps read (templates, branch conditions, for-each items)."""
    out: set[str] = set()
    common = {"name", "type", "after", "when", "retries", "retry_delay_s", "retry_backoff", "timeout_s", "description",
              "reads", "writes", "then", "else"}
    for s in steps:
        out |= expr.template_refs({k: v for k, v in s.items() if k not in common})
        if s["type"] == "branch":
            try:
                out |= expr.references(s["condition"])
            except expr.ExprError:
                pass
        if s["type"] == "for_each" and isinstance(s.get("items"), str):
            inner = expr.TEMPLATE.fullmatch(s["items"].strip())
            try:
                out |= expr.references(inner.group(1) if inner else s["items"])
            except expr.ExprError:
                pass
    return out


def native_reason(s: dict, read: set[str]) -> Optional[str]:
    """None when the step can run as a native operator, else why it is delegated to Cairn."""
    kind = s["type"]
    if kind not in NATIVE_TYPES:
        return WHY_DELEGATED.get(kind, f"a {kind} step")
    if s["name"] in read:
        return "other steps read its output, which Cairn keeps"
    fields = {k: v for k, v in s.items() if k not in ("name", "type", "after", "when", "description", "reads", "writes")}
    if not _only_params(fields):
        return "its templates read steps or the run, which Cairn fills in"
    if kind in ("sql", "check") and not (s.get("sql") and s.get("connection")):
        return "a saved query, run in Cairn's SQL workspace"
    if kind == "spark" and s.get("class_name"):
        return "a JVM application (class_name): the Spark Operator task runs Python applications"
    return None


def _common_args(s: dict) -> list[str]:
    out = [f"task_id={s['name']!r}"]
    if s.get("when", "all_ok") != "all_ok":
        out.append(f"trigger_rule={TRIGGER_RULES[s['when']]!r}")
    if s.get("retries"):
        out.append(f"retries={int(s['retries'])}")
        out.append(f"retry_delay=timedelta(seconds={_num(s.get('retry_delay_s', 30))})")
        if float(s.get("retry_backoff", 2)) > 1:
            out.append("retry_exponential_backoff=True")
    out.append(f"execution_timeout=timedelta(seconds={int(s['timeout_s'])})")
    if s.get("description"):
        out.append(f"doc_md={_py(s['description'])}")
    return out


def _num(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _native(s: dict) -> tuple[str, list[str]]:
    """(operator class, its arguments) for a native step."""
    kind = s["type"]
    if kind == "sql":
        return "CairnTrinoOperator", [f"sql={_py(s['sql'])}", "cairn_conn_id=CONN"]
    if kind == "check":
        return "CairnCheckOperator", [f"sql={_py(s['sql'])}", f"measure={s.get('measure', 'rows')!r}", f"op={s['op']!r}",
                                      f"value={_num(s['value'])}", "cairn_conn_id=CONN"]
    if kind == "dbt":
        args = [f"project_dir={s['project_dir']!r}", f"command={s['command']!r}"]
        if s.get("select"):
            args.append(f"select={_py(s['select'])}")
        return "CairnDbtOperator", args + ["cairn_conn_id=CONN"]
    if kind == "spark":
        args = [f"name={s['name']!r}", f"main={s['application']!r}"]
        if s.get("args"):
            args.append(f"arguments={_py([str(a) for a in s['args']])}")
        if s.get("conf"):
            args.append(f"spark_conf={_py(s['conf'])}")
        return "CairnSparkApplicationOperator", args + ["**SPARK"]
    if kind == "airflow":
        return "TriggerDagRunOperator", [f"trigger_dag_id={s['dag_id']!r}", f"conf={_py(s.get('conf') or {})}",
                                         f"wait_for_completion={bool(s.get('wait', True))!r}",
                                         f"poke_interval={int(s.get('poll_s', 30))}"]
    raise ValueError(kind)


WIDTH = 116


def _call(lhs: str, cls: str, args: list[str], indent: str = "    ") -> list[str]:
    """``lhs = cls(args)`` on one line when it fits, else with the arguments packed on the lines below."""
    one = f"{indent}{lhs}{cls}({', '.join(args)})"
    if len(one) <= WIDTH and "\n" not in one:
        return [one]
    out, line, pad = [f"{indent}{lhs}{cls}("], "", indent + "    "
    for a in args:
        piece = a + ","
        if "\n" in a:                                # a literal of several lines: on lines of its own
            if line:
                out.append(pad + line)
                line = ""
            out.append(pad + piece)
            continue
        if line and len(pad) + len(line) + 1 + len(piece) > WIDTH:
            out.append(pad + line)
            line = piece
        else:
            line = f"{line} {piece}" if line else piece
    if line:
        out.append(pad + line)
    return out + [f"{indent})"]


def _comment(text: str, indent: str = "") -> list[str]:
    return textwrap.wrap(text, WIDTH - len(indent) - 2, initial_indent=indent + "# ", subsequent_indent=indent + "# ",
                         break_on_hyphens=False, break_long_words=False)


def _assets(defn: dict) -> list[str]:
    """The module-level list of assets a DAG's table triggers watch (Cairn's change feed)."""
    tables = sorted({x for t in defn.get("triggers") or [] if t["type"] == "dataset" and t.get("enabled", True)
                     for x in t["tables"]})
    if not tables:
        return []
    out = ["# The tables whose changes start a run, watched in Cairn's change feed.", "ASSETS = ["]
    for i, table in enumerate(tables):
        out += [f"    Asset({table!r}, watchers=[AssetWatcher(",
                f"        name={('cairn_changed_' + str(i))!r},",
                f"        trigger=CairnDatasetChangedTrigger([{table!r}], cairn_conn_id=CONN))]),"]
    return out + ["]"]


def _schedule(defn: dict, runner: bool, lines: list[str], conn: str) -> tuple[str, set[str]]:
    """The DAG's schedule expression and the names it imports; notes on what does not map go in ``lines``."""
    if runner:
        lines.append("Runs: Cairn starts them (schedule, table changes, other flows) through Airflow's REST API, so the DAG "
                     "has no schedule of its own and every run is in Cairn's run history.")
        return "None", set()
    imports: set[str] = set()
    triggers = [t for t in defn.get("triggers") or [] if t.get("enabled", True)]
    for t in defn.get("triggers") or []:
        if not t.get("enabled", True):
            lines.append(f"Trigger off in Cairn, left out: {t['type']}")
    crons = [t for t in triggers if t["type"] == "schedule"]
    tables = sorted({x for t in triggers if t["type"] == "dataset" for x in t["tables"]})
    for t in triggers:
        if t["type"] == "flow":
            lines.append(f"Trigger after flow {t['flow']} ({', '.join(t['on'])}): no Airflow equivalent; Cairn starts "
                         "the flow after it, or use an asset the other DAG updates.")
    for t in crons[1:]:
        lines.append(f"A second schedule ({t['cron']} {t['timezone']}) is left out: Cairn, too, uses the first.")
    timetable = None
    if crons:
        imports.add("from airflow.timetables.trigger import CronTriggerTimetable")
        timetable = f"CronTriggerTimetable({crons[0]['cron']!r}, timezone={crons[0]['timezone']!r})"
    assets = None
    if tables:
        imports.add("from airflow.sdk import Asset, AssetWatcher")
        imports.add("from cairn_airflow.triggers import CairnDatasetChangedTrigger")
        assets = "ASSETS"
    if timetable and assets:
        imports.add("from airflow.timetables.assets import AssetOrTimeSchedule")
        return f"AssetOrTimeSchedule(\n        timetable={timetable},\n        assets={assets})", imports
    return timetable or assets or "None", imports


def _params(defn: dict) -> list[str]:
    out = []
    for p in defn.get("params") or []:
        t = PARAM_TYPES.get(p.get("type", "string"), "string")
        d = p.get("default")
        args = []
        templated = isinstance(d, str) and "{{" in d
        if templated:
            args.append("None")
            args.append(f"type=[{t!r}, 'null']")
        elif d is not None:
            args.append(_py(d))
            args.append(f"type={t!r}" if not (p.get("type") == "date") else "type='string', format='date'")
        elif p.get("required"):
            args.append(f"type={t!r}")
        else:
            args.append("None")
            args.append(f"type=[{t!r}, 'null']")
        desc = p.get("description") or ""
        if templated:
            desc = (desc + " " if desc else "") + f"(empty: Cairn fills in {d})"
        if desc:
            args.append(f"description={_py(desc)}")
        out.append(f"        {p['name']!r}: Param({', '.join(args)}),")
    return out


def to_dag(defn: dict, *, owner: str, version: Optional[int] = None, operators: str = "native",
           conn_id: str = "cairn_default", dag_id: Optional[str] = None, runner: bool = False) -> str:
    """The Python DAG file for a (normalized) flow definition. ``operators``: ``native`` or ``cairn`` (every step
    delegated). ``runner``: the file Cairn deploys for ``runner: airflow`` (Cairn starts the runs)."""
    if operators not in ("native", "cairn"):
        raise ValueError("operators: native or cairn")
    if runner:
        operators = "cairn"
    name = defn["name"]
    dag_id = dag_id or dag_id_for(name, runner)
    steps = defn["steps"]
    read = _read_by_others(steps)
    plan: list[tuple[dict, Optional[str]]] = []      # (step, why delegated or None)
    for s in steps:
        why = "every step runs in Cairn as the owner" if operators == "cairn" else native_reason(s, read)
        plan.append((s, why))
    imports = {"from datetime import datetime, timedelta", "from airflow.sdk import DAG"}
    provider: set[str] = set()
    other: set[str] = set()
    notes: list[str] = []
    schedule, sched_imports = _schedule(defn, runner, notes, conn_id)
    for s, why in plan:
        if s["type"] == "branch":
            provider.add("CairnFlowBranchOperator")
        elif why is not None:
            provider.add("CairnFlowStepOperator")
        else:
            cls = _native(s)[0]
            if cls == "TriggerDagRunOperator":
                other.add("from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator")
            elif cls == "CairnSparkApplicationOperator":
                other.add("from cairn_airflow.spark import CairnSparkApplicationOperator")
            elif cls == "CairnCheckOperator":
                other.add("from cairn_airflow.quality import CairnCheckOperator")
            else:
                provider.add(cls)
    params = _params(defn)
    if params:
        imports.add("from airflow.sdk import Param")
    who = owner.removeprefix("urn:li:corpuser:")
    head = [f'"""{name}: {_doc_line(defn.get("description") or "a Cairn flow")}',
            "",
            f"Generated by Cairn from the flow {name!r}" + (f" (version {version})" if version else " (draft)")
            + f", owned by {who}. Change the flow in Cairn",
            "and export it again (a flow with runner: airflow is deployed by Cairn on each publish).",
            "",
            "No secrets: tasks sign in with Airflow's connection " + repr(conn_id) + " (a Cairn token of a pipeline user).",
            "Native tasks run as that user; delegated tasks run in Cairn as the flow's owner, and need that user to be",
            "the flow's owner, an editor or an admin in Cairn. Needs " + PROVIDER + ".",
            '"""', "", "from __future__ import annotations", ""]
    body = sorted(i for i in imports if i.startswith("from datetime")) + [""]
    airflow_imports: dict[str, set[str]] = {}
    for line in sorted(imports | sched_imports | other):
        if line.startswith("from datetime"):
            continue
        mod, _, names = line[5:].partition(" import ")
        airflow_imports.setdefault(mod, set()).update(n.strip() for n in names.split(","))
    if runner:                                      # every step delegated: the same file on Airflow 3 and Airflow 2
        sdk = sorted(airflow_imports.pop("airflow.sdk", set()))
        body += ["try:  # Airflow 3", f"    from airflow.sdk import {', '.join(sdk)}", "except ImportError:  # Airflow 2",
                 "    from airflow import DAG"]
        if "Param" in sdk:
            body.append("    from airflow.models.param import Param")
    for mod in sorted(m for m in airflow_imports if not m.startswith("cairn_airflow")):
        body.append(f"from {mod} import {', '.join(sorted(airflow_imports[mod]))}")
    body.append("")
    flows_names = sorted(n for n in provider if n.startswith("CairnFlow"))
    op_names = sorted(n for n in provider if not n.startswith("CairnFlow"))
    if flows_names:
        airflow_imports.setdefault("cairn_airflow.flows", set()).update(flows_names)
    if op_names:
        airflow_imports.setdefault("cairn_airflow.operators", set()).update(op_names)
    for mod in sorted(m for m in airflow_imports if m.startswith("cairn_airflow")):
        body.append(f"from {mod} import {', '.join(sorted(airflow_imports[mod]))}")
    body += ["", f"FLOW = {name!r}", f"CONN = {conn_id!r}"]
    if any(why is None and s["type"] == "spark" for s, why in plan):
        body.insert(0, "import os")
        body += ["# The Spark Operator's settings on this Airflow (no secrets: the driver signs in with its service",
                 "# account's projected token).",
                 "SPARK = {\"image\": os.environ.get(\"CAIRN_SPARK_IMAGE\", \"\"),",
                 "         \"service_account\": os.environ.get(\"CAIRN_SPARK_SERVICE_ACCOUNT\", \"svc-spark\"),",
                 "         \"namespace\": os.environ.get(\"CAIRN_SPARK_NAMESPACE\", \"default\"),",
                 "         \"cairn_url\": os.environ.get(\"CAIRN_URL\", \"\"),",
                 "         \"catalog_uri\": os.environ.get(\"CAIRN_CATALOG_URI\", \"\")}"]
    if not runner:
        body += _assets(defn)
    body.append("")
    conc = defn.get("concurrency") or {"policy": "queue", "max_active": 1}
    if conc.get("policy") == "skip":
        notes.append("concurrency skip: Airflow queues a run while another is active (max_active_runs) rather than "
                     "skipping it.")
    if defn.get("sla_minutes"):
        notes.append(f"SLA {defn['sla_minutes']} minutes: " + ("Cairn tracks it (it starts and follows the runs)."
                                                                    if runner else "Airflow 3 has no SLAs; use a deadline "
                                                                                   "alert, or keep the flow in Cairn."))
    n = defn.get("notify") or {}
    if n.get("to") or set(n.get("on") or []) != {"failure", "sla"}:
        notes.append(f"Notifications ({', '.join(n.get('on') or [])} to {', '.join(n.get('to') or []) or 'the owner'}): "
                     + ("Cairn sends them when the run ends." if runner else "set Airflow's own callbacks for them."))
    for note in notes:
        body += _comment(note)
    tags = ["cairn", f"cairn-flow:{name}"] + ([f"cairn-flow-version:{version}"] if version else [])
    body += [
        "with DAG(",
        f"    dag_id={dag_id!r},",
        f"    description={_py(_doc_line(defn.get('description') or '')[:90])},",
        f"    schedule={schedule},",
        "    start_date=datetime(2026, 1, 1),",
        "    catchup=False,",
        f"    max_active_runs={int(conc.get('max_active', 1))},",
        f"    tags={_py(tags)},",
        f"    default_args={{'owner': {who!r}}},",
        "    is_paused_upon_creation=False,",
        "    doc_md=__doc__,",
    ]
    if params:
        body += ["    params={", *params, "    },"]
    body += [") as dag:", "    steps = {}"]
    for s, why in plan:
        args = _common_args(s)
        lhs = f"steps[{s['name']!r}] = "
        if s["type"] == "branch":
            body += _comment(f"{s['name']}: a branch; Cairn evaluates {s['condition']!r}, Airflow skips the other "
                             "side.", "    ")
            body += _call(lhs, "CairnFlowBranchOperator", args + [
                "flow=FLOW", f"step={s['name']!r}", f"then={_py(list(s['then']))}",
                f"otherwise={_py(list(s['else']))}", "cairn_conn_id=CONN"])
        elif why is not None:
            body += _comment(f"{s['name']} ({s['type']}): delegated to Cairn: {why}.", "    ")
            body += _call(lhs, "CairnFlowStepOperator", args + ["flow=FLOW", f"step={s['name']!r}",
                                                                "cairn_conn_id=CONN"])
        else:
            cls, more = _native(s)
            if s["type"] in ("sql", "check"):
                body += _comment(f"{s['name']} ({s['type']}): on Trino as the connection's user (Cairn connection "
                                 f"{s['connection']!r} in the flow).", "    ")
            body += _call(lhs, cls, args + more)
    edges = [(a, s["name"]) for s in steps for a in s["after"]]
    if edges:
        body.append("")
        for a, b in edges:
            body.append(f"    steps[{a!r}] >> steps[{b!r}]")
    return "\n".join(head + body) + "\n"


def _doc_line(text: str) -> str:
    return " ".join(str(text).split()).replace('"""', "'''")[:300]
