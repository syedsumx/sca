"""Cairn Flows: one orchestration model, a DAG of any steps, run as the flow's owner.

**Definitions and versions.** A flow keeps a draft (what the canvas or the code view saves, valid or not) and
its published versions; publishing checks the draft completely (:func:`cairn.flows.model.normalize`, then what
it refers to: derived tables the owner may refresh, jobs, agent apps, saved queries, other flows) and makes it
the next version. Runs pin the version they started on; a run of the draft is for its owner (or an admin).

**Who.** The owner, editors (edit the draft, run, cancel, rerun, backfill), viewers (see the flow and its runs)
and admins (``MANAGE_POLICIES``). Only the owner or an admin publishes, shares or deletes: a published version
is what runs as the owner. Approvers decide approval steps, whoever they are.

**Runs.** Every step runs as the owner, whoever started the run: SQL on Trino as them, jobs with their token,
agent apps as them; when that cannot be had the step fails (fail closed). A run starts manually, from the API,
on a cron in a time zone, when a table changes (the change feed: Trino's commits, pipes, streams, dbt, derived
tables, other flows), or after another flow. Parameters are typed, with defaults (which may use
``{{ run.logical_date }}``). Steps retry with backoff and have time limits; a run is cancelled, rerun from a
step, or backfilled over dates. ``concurrency``: ``skip`` (a run while one is active is recorded as skipped),
``queue`` (it waits) or ``allow`` (up to ``max_active`` at once).

**Several replicas.** A run is driven by one process at a time under a lease in the database
(``lease_owner``, ``lease_until_ms``, renewed while it runs; another process takes over a run whose lease
lapsed, and a step that was running there is retried or failed as interrupted). Scheduled starts, dataset
starts and admission are claimed with conditional updates, so each run starts once whichever replicas tick.
Waiting steps (approvals, waits, Airflow DAG runs, streams) hold no thread: the run is parked until ``wake_ms``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Index, Integer, String, Table, Text, and_, delete, func, insert, or_, select, update

from cairn.common.db import JSONText, metadata
from cairn.flows import expr, model, steps as stepmod
from cairn.flows.model import FlowError

logger = logging.getLogger(__name__)

flows = Table(
    "cairn_flows", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("editors", JSONText, nullable=False),
    Column("viewers", JSONText, nullable=False),
    Column("draft", Text, nullable=False),
    Column("draft_errors", Integer, nullable=False),
    Column("published_version", Integer, nullable=True),
    Column("enabled", Boolean, nullable=False),
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("watch", JSONText, nullable=False),          # dataset urns the published version starts on
    Column("follows", JSONText, nullable=False),        # [{flow, on}] the published version starts after
    Column("seen_change", BigInteger, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Column("updated_by", String(256), nullable=False),
)
flow_versions = Table(
    "cairn_flow_versions", metadata,
    Column("flow_id", String(32), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("definition", Text, nullable=False),
    Column("published_by", String(256), nullable=False),
    Column("published_ms", BigInteger, nullable=False),
    Column("note", String(500), nullable=True),
)
flow_runs = Table(
    "cairn_flow_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("flow_id", String(32), nullable=False),
    Column("version", Integer, nullable=False),          # 0: the draft
    Column("definition", Text, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("trigger", String(16), nullable=False),
    Column("triggered_by", String(256), nullable=True),
    Column("run_as", String(256), nullable=False),
    Column("params", JSONText, nullable=False),
    Column("logical_date", String(10), nullable=True),
    Column("detail", JSONText, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("started_ms", BigInteger, nullable=True),
    Column("finished_ms", BigInteger, nullable=True),
    Column("wake_ms", BigInteger, nullable=True),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_until_ms", BigInteger, nullable=True),
    Column("cancel_requested", Boolean, nullable=False),
    Column("sla_alerted", Boolean, nullable=False),
    Column("error", Text, nullable=True),
    Index("ix_cairn_flow_runs_flow_created", "flow_id", "created_ms"),
)
flow_step_runs = Table(
    "cairn_flow_step_runs", metadata,
    Column("run_id", String(32), primary_key=True),
    Column("key", String(140), primary_key=True),       # the step's name; a for-each's items are name[i]
    Column("step", String(64), nullable=False),
    Column("parent", String(64), nullable=True),
    Column("type", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("started_ms", BigInteger, nullable=True),
    Column("finished_ms", BigInteger, nullable=True),
    Column("next_ms", BigInteger, nullable=True),
    Column("output", Text, nullable=True),
    Column("log", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("state", JSONText, nullable=True),
    Column("decision", JSONText, nullable=True),
)

ACTIVE = ("queued", "running", "waiting")
DONE = ("succeeded", "failed", "cancelled", "skipped")
STEP_DONE = ("ok", "failed", "skipped", "cancelled")
LINEAGE_NS = "cairn-flows"


def _now() -> int:
    return int(time.time() * 1000)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(os.environ.get(name, str(default))), hi))
    except ValueError:
        return default


def next_fire(cron: str, tz: str, after_ms: int) -> int:
    """The next time ``cron`` fires in time zone ``tz`` after ``after_ms`` (epoch ms)."""
    from zoneinfo import ZoneInfo

    from croniter import croniter

    base = datetime.fromtimestamp(after_ms / 1000, tz=ZoneInfo(tz))
    return int(croniter(cron, base).get_next(datetime).timestamp() * 1000)


def _invalid(errors: list[dict]) -> str:
    first = errors[0] if errors else {"path": "", "message": "is not valid"}
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"the flow is not valid: {first.get('path') or 'flow'}: {first['message']}{more}"


def _bounded(value: Any, cap: int) -> Optional[str]:
    """JSON of an output, at most ``cap`` bytes: long lists and texts are cut, and it says so."""
    if value is None:
        return None
    text = json.dumps(value, default=str)
    if len(text) <= cap:
        return text
    v = copy.deepcopy(value) if isinstance(value, dict) else {"value": value}
    for k in list(v):
        if isinstance(v[k], list):
            v[k] = v[k][:20]
        elif isinstance(v[k], str) and len(v[k]) > 4000:
            v[k] = v[k][:4000] + "…"
    v["truncated"] = True
    text = json.dumps(v, default=str)
    if len(text) <= cap:
        return text
    return json.dumps({"truncated": True, "summary": text[: max(0, cap - 100)]})


class Flows:
    def __init__(self, engine, backends: stepmod.Backends, audit=None,
                 notify: Optional[Callable[..., Any]] = None, is_admin: Optional[Callable[[str], bool]] = None,
                 groups_of: Optional[Callable[[str], list[str]]] = None, changes=None,
                 table_urn: Optional[Callable[[str], str]] = None, lineage: Optional[Callable[[dict], Any]] = None,
                 may_run: Optional[Callable[[str], Optional[str]]] = None, instance: Optional[str] = None):
        """``notify(type, title, body, recipients, payload)``; ``changes``: DatasetChanges (dataset triggers);
        ``table_urn(name)``: the dataset urn of ``schema.table``; ``lineage(openlineage_event)``;
        ``may_run(owner)``: None when the owner may run flows now, else why not (raising counts as no)."""
        self.engine, self.b, self.audit, self.notify = engine, backends, audit, notify
        self.is_admin = is_admin or (lambda actor: False)
        self.groups_of = groups_of or (lambda actor: [])
        self.changes, self.lineage, self.may_run = changes, lineage, may_run
        self.table_urn = table_urn or (lambda t: t if t.startswith("urn:li:dataset:")
                                       else f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)")
        self.instance = instance or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_ms = _int_env("CAIRN_FLOWS_LEASE_SECONDS", 60, 10, 3600) * 1000
        self.max_output = _int_env("CAIRN_FLOWS_MAX_OUTPUT_BYTES", 65536, 1024, 1 << 22)
        self.max_log = _int_env("CAIRN_FLOWS_MAX_LOG_BYTES", 65536, 1024, 1 << 22)
        self.keep_days = _int_env("CAIRN_FLOWS_KEEP_DAYS", 90, 1, 3650)
        self.min_interval = _int_env("CAIRN_FLOWS_MIN_INTERVAL_MINUTES", 5, 1, 1440)
        self.pool = ThreadPoolExecutor(max_workers=_int_env("CAIRN_FLOWS_WORKERS", 8, 1, 256), thread_name_prefix="cairn-flow")
        self._lock = threading.Lock()
        self._driving: dict[str, threading.Event] = {}
        self._last_sweep = 0

    # ---- who may do what ------------------------------------------------------------------------------------
    def _role(self, actor: str, row: dict) -> Optional[str]:
        if actor == row["owner"]:
            return "owner"
        if self.is_admin(actor):
            return "admin"
        mine = {actor} | {f"urn:li:corpGroup:{g}" if not str(g).startswith("urn:li:") else str(g)
                          for g in self._groups(actor)}
        if mine & set(row["editors"] or []):
            return "editor"
        if mine & set(row["viewers"] or []):
            return "viewer"
        return None

    def _groups(self, actor: str) -> list[str]:
        try:
            return list(self.groups_of(actor) or [])
        except Exception:  # noqa: BLE001 - no groups known: only what is granted to the person counts
            logger.warning("flows: could not resolve the groups of %s", actor)
            return []

    def _need(self, actor: str, row: dict, what: str) -> str:
        role = self._role(actor, row)
        allowed = {"view": ("owner", "admin", "editor", "viewer"), "edit": ("owner", "admin", "editor"),
                   "run": ("owner", "admin", "editor"), "own": ("owner", "admin")}[what]
        if role is None:
            raise FlowError("no such flow", 404)
        if role not in allowed:
            verb = {"edit": "edit", "run": "run", "own": "publish, share or delete"}[what]
            raise FlowError(f"only the flow's owner{', editors' if what in ('edit', 'run') else ''} or an admin may "
                            f"{verb} {row['name']}", 403)
        return role

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            try:
                self.audit.record("flows", action, actor, f"flow:{name}", details)
            except Exception:  # noqa: BLE001 - logged; the change itself stands
                logger.exception("flows: audit %s failed", action)

    # ---- definitions ----------------------------------------------------------------------------------------
    def _row(self, ref: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(flows).where(or_(flows.c.id == ref, flows.c.name == ref))).first()
        if r is None:
            raise FlowError("no such flow", 404)
        return dict(r._mapping)

    @staticmethod
    def _split(body: Any) -> tuple[Any, dict]:
        """(definition, sharing) from an API body: the definition itself, ``{"yaml": text}``, or
        ``{"definition": ..., "editors": [...], "viewers": [...]}``."""
        if isinstance(body, str):
            return model.from_text(body), {}
        if not isinstance(body, dict):
            raise FlowError("a flow is a mapping")
        if "yaml" in body and isinstance(body["yaml"], str):
            return model.from_text(body["yaml"]), {k: body[k] for k in ("editors", "viewers") if k in body}
        if "definition" in body:
            d = body["definition"]
            return (model.from_text(d) if isinstance(d, str) else d), {k: body[k] for k in ("editors", "viewers") if k in body}
        if isinstance(body.get("spec"), dict):         # the IaC shape (bundles, Terraform): {name, spec, enabled}
            spec = {**body["spec"], **({"name": body["name"]} if body.get("name") else {})}
            opts = {k: body[k] for k in ("editors", "viewers") if k in body}
            if "enabled" in body:
                opts["_enabled"] = bool(body["enabled"])
            if body.get("publish"):
                opts["_publish"] = True
            return spec, opts
        return body, {}

    @staticmethod
    def _people(v: Any, what: str) -> list[str]:
        if not isinstance(v, list) or len(v) > 100 or not all(isinstance(x, str) and model.URN_USER.match(x) for x in v):
            raise FlowError(f"{what}: a list of people and groups (urn:li:corpuser:… or urn:li:corpGroup:…)")
        return sorted(set(v))

    def create(self, actor: str, body: Any, publish: bool = False, note: Optional[str] = None) -> dict:
        raw, sharing = self._split(body)
        publish, enabled = publish or sharing.pop("_publish", False), sharing.pop("_enabled", None)
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not model.NAME.match(raw["name"]):
            raise FlowError("name: lowercase letters, digits, - and _ (starting with a letter)", 400,
                            [{"path": "name", "message": "is lowercase letters, digits, - and _"}])
        check = model.validate(raw)
        if publish and not check["valid"]:
            raise FlowError(_invalid(check["errors"]), 400, check["errors"])
        now, fid = _now(), uuid.uuid4().hex
        draft = check["flow"] or raw
        with self.engine.begin() as conn:
            if conn.execute(select(flows.c.id).where(flows.c.name == raw["name"])).first():
                raise FlowError(f"a flow named {raw['name']} exists", 409)
            conn.execute(insert(flows).values(
                id=fid, name=raw["name"], owner=actor, editors=self._people(sharing.get("editors", []), "editors"),
                viewers=self._people(sharing.get("viewers", []), "viewers"), draft=json.dumps(draft, default=str),
                draft_errors=len(check["errors"]), published_version=None, enabled=True, next_run_ms=None, watch=[],
                follows=[], seen_change=0, created_ms=now, updated_ms=now, updated_by=actor))
        self._audit(actor, "flow.created", raw["name"], {"valid": check["valid"]})
        if publish:
            self.publish(actor, fid, note)
        if enabled is False:
            self.set_enabled(actor, fid, False)
        return self.get(actor, fid)

    def save(self, actor: str, ref: str, body: Any, publish: bool = False, note: Optional[str] = None) -> dict:
        """Saves the draft (valid or not); with ``publish``, publishes it too."""
        row = self._row(ref)
        self._need(actor, row, "edit")
        raw, sharing = self._split(body)
        publish, enabled = publish or sharing.pop("_publish", False), sharing.pop("_enabled", None)
        if not isinstance(raw, dict):
            raise FlowError("a flow is a mapping")
        if raw.get("name", row["name"]) != row["name"]:
            raise FlowError(f"a flow's name does not change ({row['name']}); make a new flow for a new name", 400,
                            [{"path": "name", "message": f"is {row['name']}"}])
        raw = {**raw, "name": row["name"]}
        check = model.validate(raw)
        if publish and not check["valid"]:
            raise FlowError(_invalid(check["errors"]), 400, check["errors"])
        with self.engine.begin() as conn:
            conn.execute(update(flows).where(flows.c.id == row["id"]).values(
                draft=json.dumps(check["flow"] or raw, default=str), draft_errors=len(check["errors"]),
                updated_ms=_now(), updated_by=actor))
        self._audit(actor, "flow.saved", row["name"], {"valid": check["valid"]})
        if sharing:
            self.share(actor, row["id"], sharing.get("editors", row["editors"]), sharing.get("viewers", row["viewers"]))
        if publish:
            self.publish(actor, row["id"], note)
        if enabled is not None and enabled != bool(row["enabled"]):
            self.set_enabled(actor, row["id"], enabled)
        return self.get(actor, row["id"])

    def share(self, actor: str, ref: str, editors: list, viewers: list) -> dict:
        row = self._row(ref)
        self._need(actor, row, "own")
        e, v = self._people(editors, "editors"), self._people(viewers, "viewers")
        with self.engine.begin() as conn:
            conn.execute(update(flows).where(flows.c.id == row["id"]).values(editors=e, viewers=v, updated_ms=_now()))
        self._audit(actor, "flow.shared", row["name"], {"editors": e, "viewers": v})
        return self.get(actor, row["id"])

    def _deep_check(self, owner: str, defn: dict) -> None:
        """What a definition refers to exists and is the owner's to use; FlowError listing each problem."""
        errs = model._Errors()
        b = self.b
        writes: set[str] = set()
        for i, s in enumerate(defn["steps"]):
            p, kind = f"steps[{i}]", s["type"]
            body = s["step"] if kind == "for_each" and s.get("step") else s
            k = body["type"] if kind == "for_each" else kind
            writes |= {self.table_urn(t) for t in s.get("writes") or []}
            try:
                if k == "derived" and not expr.TEMPLATE.search(body["table"]):
                    if b.derived is None:
                        raise stepmod.StepFailed("derived tables are not set up here")
                    dt = next((d for d in b.derived.list() if d["name"] == body["table"]), None)
                    if dt is None:
                        raise stepmod.StepFailed(f"no derived table {body['table']!r}")
                    if dt["owner"] != owner and not b.is_operator(owner):
                        raise stepmod.StepFailed(f"{dt['name']} is not the owner's to refresh")
                    writes.add(self.table_urn(dt["target"]) if dt.get("target") else "")
                if k in ("sql", "check") and body.get("query") and not expr.TEMPLATE.search(body["query"]):
                    stepmod.resolve_query(stepmod.need(b.workspace, "the SQL workspace"), owner, body["query"])
                if k in ("python", "notebook", "spark", "dbt", "airflow"):
                    jobs = stepmod.need(b.jobs, "jobs")
                    if not jobs.allowed(owner):
                        raise stepmod.StepFailed(f"{k} steps need RUN_JOBS, which the owner does not have")
                    if body.get("job"):
                        j = jobs._get(body["job"])
                        if j["owner"] != owner and not b.is_operator(owner):
                            raise stepmod.StepFailed(f"the job {body['job']} is not the owner's")
                if k == "agent" and not expr.TEMPLATE.search(body["app"]):
                    stepmod.need(b.agent_apps, "agent apps").get(owner, body["app"])
                if k == "pipe":
                    if not b.is_operator(owner):
                        raise stepmod.StepFailed("loading a pipe needs MANAGE_SYSTEM_OPERATIONS")
                    if not any(x["name"] == body["pipe"] for x in stepmod.need(b.pipes, "pipes").list()):
                        raise stepmod.StepFailed(f"no pipe {body['pipe']!r}")
                if k == "stream":
                    stepmod.need(b.streams, "streams").by_name(body["stream"])
            except stepmod.StepFailed as e:
                errs.add(p, str(e), s["name"])
            except Exception as e:  # noqa: BLE001 - a lookup that refuses (404, 403) is a problem with the step
                errs.add(p, str(e) or type(e).__name__, s["name"])
        for i, t in enumerate(defn["triggers"]):
            if t["type"] == "schedule":
                a = next_fire(t["cron"], t["timezone"], _now())
                if next_fire(t["cron"], t["timezone"], a) - a < self.min_interval * 60_000:
                    errs.add(f"triggers[{i}].cron", f"runs at most every {self.min_interval} minutes")
            if t["type"] == "dataset":
                loop = sorted({self.table_urn(x) for x in t["tables"]} & writes)
                if loop:
                    errs.add(f"triggers[{i}].tables", f"the flow writes {loop[0]}, so it would start itself")
            if t["type"] == "flow":
                try:
                    other = self._row(t["flow"])
                    self._need(owner, other, "view")
                except FlowError:
                    errs.add(f"triggers[{i}].flow", f"no flow {t['flow']!r} the owner may see")
        errs.raise_if_any()

    def publish(self, actor: str, ref: str, note: Optional[str] = None) -> dict:
        row = self._row(ref)
        self._need(actor, row, "own")
        defn = model.normalize(json.loads(row["draft"]))
        self._deep_check(row["owner"], defn)
        now = _now()
        sched = next((t for t in defn["triggers"] if t["type"] == "schedule" and t["enabled"]), None)
        watch = sorted({self.table_urn(x) for t in defn["triggers"] if t["type"] == "dataset" and t["enabled"]
                        for x in t["tables"]})
        follows = [{"flow": t["flow"], "on": t["on"]} for t in defn["triggers"] if t["type"] == "flow" and t["enabled"]]
        with self.engine.begin() as conn:
            current = conn.execute(select(func.max(flow_versions.c.version)).where(flow_versions.c.flow_id == row["id"])).scalar()
            version = int(current or 0) + 1
            conn.execute(insert(flow_versions).values(flow_id=row["id"], version=version, definition=json.dumps(defn),
                                                      published_by=actor, published_ms=now, note=(note or "")[:500] or None))
            seen = row["seen_change"]
            if watch and not row["watch"] and self.changes is not None:
                seen = self.changes.head()           # changes from now on, not the backlog
            conn.execute(update(flows).where(flows.c.id == row["id"]).values(
                published_version=version, updated_ms=now, updated_by=actor, watch=watch, follows=follows,
                seen_change=seen, next_run_ms=next_fire(sched["cron"], sched["timezone"], now) if sched else None))
        self._audit(actor, "flow.published", row["name"], {"version": version, "steps": len(defn["steps"])})
        return {"flow": row["name"], "version": version}

    def set_enabled(self, actor: str, ref: str, enabled: bool) -> dict:
        row = self._row(ref)
        self._need(actor, row, "own")
        values: dict[str, Any] = {"enabled": bool(enabled), "updated_ms": _now()}
        if enabled and row["published_version"]:
            defn = self._version(row["id"], row["published_version"])
            sched = next((t for t in defn["triggers"] if t["type"] == "schedule" and t["enabled"]), None)
            values["next_run_ms"] = next_fire(sched["cron"], sched["timezone"], _now()) if sched else None
        with self.engine.begin() as conn:
            conn.execute(update(flows).where(flows.c.id == row["id"]).values(**values))
        self._audit(actor, "flow.enabled" if enabled else "flow.disabled", row["name"], {})
        return self.get(actor, row["id"])

    def delete(self, actor: str, ref: str) -> None:
        row = self._row(ref)
        self._need(actor, row, "own")
        with self.engine.begin() as conn:
            ids = [r[0] for r in conn.execute(select(flow_runs.c.id).where(flow_runs.c.flow_id == row["id"]))]
            for chunk in range(0, len(ids), 500):
                conn.execute(delete(flow_step_runs).where(flow_step_runs.c.run_id.in_(ids[chunk:chunk + 500])))
            conn.execute(delete(flow_runs).where(flow_runs.c.flow_id == row["id"]))
            conn.execute(delete(flow_versions).where(flow_versions.c.flow_id == row["id"]))
            conn.execute(delete(flows).where(flows.c.id == row["id"]))
        self._audit(actor, "flow.deleted", row["name"], {})

    def _version(self, fid: str, version: int) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(flow_versions.c.definition).where(
                and_(flow_versions.c.flow_id == fid, flow_versions.c.version == version))).first()
        if r is None:
            raise FlowError(f"no version {version}", 404)
        return json.loads(r.definition)

    def _summary(self, row: dict, actor: str) -> dict:
        with self.engine.connect() as conn:
            last = conn.execute(select(flow_runs.c.id, flow_runs.c.status, flow_runs.c.created_ms, flow_runs.c.finished_ms)
                                .where(flow_runs.c.flow_id == row["id"]).order_by(flow_runs.c.created_ms.desc()).limit(1)).first()
        draft = json.loads(row["draft"])
        return {"id": row["id"], "name": row["name"], "owner": row["owner"], "description": draft.get("description") or "",
                "editors": row["editors"], "viewers": row["viewers"], "enabled": bool(row["enabled"]),
                "published_version": row["published_version"], "draft_errors": row["draft_errors"],
                "next_run_ms": row["next_run_ms"], "updated_ms": row["updated_ms"], "updated_by": row["updated_by"],
                "steps": len(draft.get("steps") or []), "role": self._role(actor, row),
                "last_run": dict(last._mapping) if last else None}

    def get(self, actor: str, ref: str) -> dict:
        row = self._row(ref)
        self._need(actor, row, "view")
        draft = json.loads(row["draft"])
        check = model.validate(draft)
        published = self._version(row["id"], row["published_version"]) if row["published_version"] else None
        return {**self._summary(row, actor), "draft": draft, "errors": check["errors"],
                "draft_yaml": model.to_yaml(check["flow"]) if check["valid"] else None,
                "published": published, "spec": published,
                "changes": model.diff(published, check["flow"]) if check["valid"] else None}

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(flows).order_by(flows.c.name))]
        out = []
        for r in rows:
            if self._role(actor, r) is not None:
                s = self._summary(r, actor)
                s["spec"] = self._version(r["id"], r["published_version"]) if r["published_version"] else None
                out.append(s)
        return out

    def versions(self, actor: str, ref: str) -> list[dict]:
        row = self._row(ref)
        self._need(actor, row, "view")
        with self.engine.connect() as conn:
            rows = conn.execute(select(flow_versions.c.version, flow_versions.c.published_by, flow_versions.c.published_ms,
                                       flow_versions.c.note).where(flow_versions.c.flow_id == row["id"])
                                .order_by(flow_versions.c.version.desc())).all()
        return [dict(r._mapping) for r in rows]

    def version(self, actor: str, ref: str, version: int) -> dict:
        row = self._row(ref)
        self._need(actor, row, "view")
        d = self._version(row["id"], version)
        return {"version": version, "definition": d, "yaml": model.to_yaml(d)}

    def diff(self, actor: str, ref: str, a: Optional[int] = None, b: Optional[str] = None) -> dict:
        """What changes from version ``a`` (default: the published one) to ``b`` (a version, default the draft)."""
        row = self._row(ref)
        self._need(actor, row, "view")
        old = self._version(row["id"], a) if a else (self._version(row["id"], row["published_version"])
                                                     if row["published_version"] else None)
        if b in (None, "draft"):
            new = model.normalize(json.loads(row["draft"]))
        else:
            new = self._version(row["id"], int(b))
        return model.diff(old, new)

    # ---- starting runs ----------------------------------------------------------------------------------------
    def _params(self, defn: dict, given: Optional[dict], run_values: dict, strict: bool = True) -> dict:
        given = dict(given or {})
        out = {}
        declared = {p["name"]: p for p in defn["params"]}
        unknown = sorted(set(given) - set(declared))
        if unknown and strict:
            raise FlowError(f"unknown parameter {unknown[0]} (the flow takes {', '.join(declared) or 'none'})")
        for name, p in declared.items():
            v = given.get(name, p["default"])
            try:
                if isinstance(v, str) and expr.TEMPLATE.search(v):
                    v = expr.render(v, {"run": run_values, "flow": {"name": defn["name"]}, "params": out})
                v = model.coerce(p["type"], v)
            except (ValueError, expr.ExprError) as e:
                raise FlowError(f"parameter {name}: {e}")
            if v is None and p["required"]:
                raise FlowError(f"parameter {name} is required")
            out[name] = v
        return out

    def _new_run(self, row: dict, version: int, defn: dict, trigger: str, by: Optional[str], params: Optional[dict],
                 logical_date: Optional[str] = None, detail: Optional[dict] = None, force_queue: bool = False,
                 reuse: Optional[dict[str, dict]] = None) -> str:
        rid, now = uuid.uuid4().hex, _now()
        logical_date = logical_date or datetime.now(timezone.utc).date().isoformat()
        values = self._params(defn, params, {"id": rid, "logical_date": logical_date, "trigger": trigger})
        status, error = "queued", None
        why = None
        try:
            why = self.may_run(row["owner"]) if self.may_run is not None else None
        except Exception as e:  # noqa: BLE001 - cannot tell: refuse
            why = f"Cairn cannot check {row['owner']} now ({type(e).__name__})"
        if why:
            status, error = "failed", f"not run: {why}"
        with self.engine.begin() as conn:
            conn.execute(update(flows).where(flows.c.id == row["id"]).values(updated_ms=flows.c.updated_ms))  # per-flow lock
            if status == "queued" and defn["concurrency"]["policy"] == "skip" and not force_queue:
                active = conn.execute(select(func.count()).select_from(flow_runs).where(
                    and_(flow_runs.c.flow_id == row["id"], flow_runs.c.status.in_(ACTIVE)))).scalar()
                if active:
                    status, error = "skipped", "another run was in progress (concurrency: skip)"
            conn.execute(insert(flow_runs).values(
                id=rid, flow_id=row["id"], version=version, definition=json.dumps(defn), status=status, trigger=trigger,
                triggered_by=by, run_as=row["owner"], params=values, logical_date=logical_date, detail=detail or {},
                created_ms=now, started_ms=None, finished_ms=now if status in DONE else None, wake_ms=None,
                lease_owner=None, lease_until_ms=None, cancel_requested=False, sla_alerted=False, error=error))
            conn.execute(insert(flow_step_runs), [
                {"run_id": rid, "key": s["name"], "step": s["name"], "parent": None, "type": s["type"],
                 **({"status": "pending", "attempt": 0, "started_ms": None, "finished_ms": None, "next_ms": None,
                     "output": None, "log": None, "error": None, "state": None, "decision": None}
                    if not (reuse and s["name"] in reuse) else reuse[s["name"]])}
                for s in defn["steps"]])
        self._audit(by or row["owner"], "flow.run_" + ("started" if status == "queued" else status), row["name"],
                    {"run": rid, "trigger": trigger, "version": version, **({"error": error} if error else {})})
        if status == "failed":
            self._tell(row, defn, rid, "failed", error or "")
        return rid

    def run(self, actor: str, ref: str, params: Optional[dict] = None, version: Optional[Any] = None,
            trigger: str = "manual", wait: bool = False, timeout_s: float = 600) -> dict:
        """Starts a run of the published version (or ``version``: a number, or ``draft`` for the owner)."""
        row = self._row(ref)
        self._need(actor, row, "run")
        if trigger not in ("manual", "api", "agent"):
            trigger = "api"
        if version == "draft":
            self._need(actor, row, "own")
            defn, v = model.normalize(json.loads(row["draft"])), 0
        else:
            v = int(version or row["published_version"] or 0)
            if not v:
                raise FlowError(f"{row['name']} has no published version: publish it (or run the draft)", 409)
            defn = self._version(row["id"], v)
        if not row["enabled"] and version != "draft":
            raise FlowError(f"{row['name']} is turned off", 409)
        rid = self._new_run(row, v, defn, trigger, actor, params)
        self._admit(row["id"])
        if wait:
            return self.wait_for(rid, timeout_s)
        return self._run_public(rid)

    def wait_for(self, rid: str, timeout_s: float = 600) -> dict:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            r = self._run_public(rid)
            if r["status"] in DONE:
                return r
            time.sleep(0.2)
        return self._run_public(rid)

    def backfill(self, actor: str, ref: str, start: str, end: str, param: Optional[str] = None,
                 params: Optional[dict] = None) -> list[str]:
        """One run per day from ``start`` to ``end`` (inclusive, at most 366), each with its logical date (and
        parameter ``param`` set to it). Backfill runs queue whatever the concurrency policy (max_active holds)."""
        row = self._row(ref)
        self._need(actor, row, "run")
        if not row["published_version"]:
            raise FlowError(f"{row['name']} has no published version", 409)
        defn = self._version(row["id"], row["published_version"])
        try:
            a, z = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError):
            raise FlowError("start and end are dates (YYYY-MM-DD)")
        if z < a or (z - a).days >= 366:
            raise FlowError("a backfill is 1 to 366 days, start before end")
        if param is not None and not any(p["name"] == param for p in defn["params"]):
            raise FlowError(f"the flow has no parameter {param}")
        ids = []
        d = a
        while d <= z:
            ps = {**(params or {}), **({param: d.isoformat()} if param else {})}
            ids.append(self._new_run(row, row["published_version"], defn, "backfill", actor, ps, d.isoformat(),
                                     {"backfill": {"start": start, "end": end}}, force_queue=True))
            d += timedelta(days=1)
        self._audit(actor, "flow.backfill", row["name"], {"start": start, "end": end, "runs": len(ids)})
        self._admit(row["id"])
        return ids

    def rerun(self, actor: str, rid: str, from_step: Optional[str] = None) -> dict:
        """A new run of the same version and parameters: ``from_step`` and everything after it run again (without it,
        what did not succeed and everything after that); the rest keeps its outputs."""
        run, row = self._run_row(rid), None
        row = self._row(run["flow_id"])
        self._need(actor, row, "run")
        if run["status"] in ACTIVE:
            raise FlowError("the run has not finished", 409)
        defn = json.loads(run["definition"])
        names = [s["name"] for s in defn["steps"]]
        rows = {r["key"]: r for r in self._step_rows(rid) if r["parent"] is None}
        if from_step is not None and from_step not in names:
            raise FlowError(f"no step {from_step!r} in that run", 404)
        roots = {from_step} if from_step else {n for n in names if rows.get(n, {}).get("status") not in ("ok", "skipped")}
        children: dict[str, set[str]] = {n: set() for n in names}
        for s in defn["steps"]:
            for a in s["after"]:
                children[a].add(s["name"])
        again, todo = set(), list(roots)
        while todo:
            n = todo.pop()
            if n not in again:
                again.add(n)
                todo.extend(children[n])
        reuse = {}
        for n in names:
            if n in again:
                continue
            r = rows[n]
            reuse[n] = {"status": r["status"], "attempt": r["attempt"], "started_ms": r["started_ms"],
                        "finished_ms": r["finished_ms"], "next_ms": None, "output": r["output"], "log": r["log"],
                        "error": r["error"], "state": {"reused_from": rid}, "decision": r["decision"]}
        new = self._new_run(row, run["version"], defn, "rerun", actor, run["params"], run["logical_date"],
                            {"rerun_of": rid, "from_step": from_step}, reuse=reuse)
        self._admit(row["id"])
        return self._run_public(new)

    # ---- admission and driving ----------------------------------------------------------------------------------
    def _admit(self, fid: str) -> list[str]:
        """Starts queued runs of a flow while it has room (max_active); claimed, so replicas start each once."""
        started = []
        with self.engine.begin() as conn:
            conn.execute(update(flows).where(flows.c.id == fid).values(updated_ms=flows.c.updated_ms))  # per-flow lock
            active = conn.execute(select(func.count()).select_from(flow_runs).where(
                and_(flow_runs.c.flow_id == fid, flow_runs.c.status.in_(("running", "waiting"))))).scalar()
            queued = conn.execute(select(flow_runs.c.id, flow_runs.c.definition).where(
                and_(flow_runs.c.flow_id == fid, flow_runs.c.status == "queued")).order_by(flow_runs.c.created_ms)).all()
            for r in queued:
                cap = json.loads(r.definition)["concurrency"]["max_active"]
                if active >= cap:
                    break
                if conn.execute(update(flow_runs).where(and_(flow_runs.c.id == r.id, flow_runs.c.status == "queued"))
                                .values(status="running", started_ms=_now())).rowcount:
                    active += 1
                    started.append(r.id)
        for rid in started:
            self.kick(rid)
        return started

    def kick(self, rid: str) -> None:
        """Drives a run in this process (unless it already is): a new thread, which leaves when the run parks."""
        with self._lock:
            ev = self._driving.get(rid)
            if ev is not None:
                ev.set()
                return
            self._driving[rid] = threading.Event()
        threading.Thread(target=self._drive_safe, args=(rid,), daemon=True, name=f"cairn-flow-run-{rid[:8]}").start()

    def _drive_safe(self, rid: str) -> None:
        try:
            self._drive(rid)
        except Exception:  # noqa: BLE001 - the lease lapses and another tick takes the run over
            logger.exception("flow run %s: driver failed", rid)
        finally:
            with self._lock:
                ev = self._driving.pop(rid, None)
            if ev is not None and ev.is_set():          # a decision or cancel arrived as it parked: look again
                self.kick(rid)

    def _claim(self, rid: str) -> bool:
        now = _now()
        with self.engine.begin() as conn:
            return bool(conn.execute(update(flow_runs).where(and_(
                flow_runs.c.id == rid, flow_runs.c.status.in_(("running", "waiting")),
                or_(flow_runs.c.lease_owner.is_(None), flow_runs.c.lease_owner == self.instance,
                    flow_runs.c.lease_until_ms < now)))
                .values(lease_owner=self.instance, lease_until_ms=now + self.lease_ms, status="running")).rowcount)

    def _renew(self, rid: str) -> bool:
        with self.engine.begin() as conn:
            return bool(conn.execute(update(flow_runs).where(and_(flow_runs.c.id == rid, flow_runs.c.lease_owner == self.instance))
                                     .values(lease_until_ms=_now() + self.lease_ms)).rowcount)

    def _release(self, rid: str, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(flow_runs).where(and_(flow_runs.c.id == rid, flow_runs.c.lease_owner == self.instance))
                         .values(lease_owner=None, lease_until_ms=None, **values))

    def _run_row(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(flow_runs).where(flow_runs.c.id == rid)).first()
        if r is None:
            raise FlowError("no such run", 404)
        return dict(r._mapping)

    def _step_rows(self, rid: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(flow_step_runs).where(flow_step_runs.c.run_id == rid))]

    def _set_step(self, rid: str, key: str, **values: Any) -> None:
        if "output" in values and not isinstance(values["output"], (str, type(None))):
            values["output"] = _bounded(values["output"], self.max_output)
        if "log" in values and values["log"] is not None:
            values["log"] = values["log"][-self.max_log:]
        if "error" in values and values["error"] is not None:
            values["error"] = str(values["error"])[:4000]
        with self.engine.begin() as conn:
            conn.execute(update(flow_step_runs).where(and_(flow_step_runs.c.run_id == rid, flow_step_runs.c.key == key))
                         .values(**values))

    def _values(self, run: dict, defn: dict, rows: dict[str, dict]) -> dict:
        out: dict[str, Any] = {}
        for n, r in rows.items():
            if r["status"] in STEP_DONE:
                o = json.loads(r["output"]) if r["output"] else {}
                out[n] = {**(o if isinstance(o, dict) else {"value": o}), "status": r["status"], "error": r["error"]}
        return {"params": run["params"] or {}, "steps": out, "flow": {"name": defn["name"]},
                "run": {"id": run["id"], "logical_date": run["logical_date"], "trigger": run["trigger"]}}

    @staticmethod
    def _tz(defn: dict) -> str:
        return next((t["timezone"] for t in defn["triggers"] if t["type"] == "schedule"), "UTC")

    def _drive(self, rid: str) -> None:
        if not self._claim(rid):
            return
        futures: dict[str, tuple[Future, stepmod.Ctx]] = {}
        cancel = threading.Event()
        last_renew = _now()
        while True:
            now = _now()
            if now - last_renew > self.lease_ms / 3:
                if not self._renew(rid):
                    cancel.set()                         # another process took the run over
                    return
                last_renew = now
            run = self._run_row(rid)
            if run["status"] not in ("running", "waiting") or run["lease_owner"] != self.instance:
                return
            defn = json.loads(run["definition"])
            by = {s["name"]: s for s in defn["steps"]}
            rows = {r["key"]: r for r in self._step_rows(rid) if r["parent"] is None}
            if run["cancel_requested"]:
                cancel.set()
                for key, r in rows.items():
                    if r["status"] not in STEP_DONE:
                        self._set_step(rid, key, status="cancelled", finished_ms=now, error="the run was cancelled")
                self._finish(rid, "cancelled")
                return
            # Steps that finished (or ran out of time) in this process.
            for key, (fut, ctx) in list(futures.items()):
                if fut.done():
                    del futures[key]
                    self._apply(rid, by[key], rows[key], ctx, fut)
                elif now > ctx.deadline_ms and by[key]["type"] not in stepmod.WAITING:
                    del futures[key]
                    ctx.cancelled.set()
                    self._failed(rid, by[key], rows[key], f"timed out after {by[key]['timeout_s']} s", ctx.lines)
            rows = {r["key"]: r for r in self._step_rows(rid) if r["parent"] is None}
            for key, r in rows.items():
                if r["status"] == "waiting" and by[key]["type"] not in ("approval", "wait") and r["started_ms"] \
                        and now > r["started_ms"] + by[key]["timeout_s"] * 1000:
                    self._failed(rid, by[key], r, f"timed out after {by[key]['timeout_s']} s", [])
                elif r["status"] == "running" and key not in futures:
                    if by[key]["type"] in stepmod.WAITING:    # looking again is safe: it resumes from its state
                        self._set_step(rid, key, status="waiting", next_ms=now)
                    else:
                        self._failed(rid, by[key], r, "interrupted: the Cairn process running it stopped", [])
                elif r["status"] == "retrying" and (r["next_ms"] or 0) <= now:
                    self._set_step(rid, key, status="pending")
            rows = {r["key"]: r for r in self._step_rows(rid) if r["parent"] is None}
            values = self._values(run, defn, rows)
            for s in model.order(defn["steps"]):
                key, r = s["name"], rows[s["name"]]
                if r["status"] == "waiting" and (r["next_ms"] or 0) <= now and key not in futures:
                    futures[key] = self._submit(rid, run, defn, s, r, values, cancel, resume=True)
                    continue
                if r["status"] != "pending":
                    continue
                deps = [rows[a] for a in s["after"]]
                if any(d["status"] not in STEP_DONE for d in deps):
                    continue
                skip = self._skip_reason(s, deps, by, rows)
                if skip:
                    self._set_step(rid, key, status="skipped", finished_ms=now, error=skip)
                    rows[key] = {**r, "status": "skipped", "error": skip}
                    continue
                futures[key] = self._submit(rid, run, defn, s, r, values, cancel)
            rows = {r["key"]: r for r in self._step_rows(rid) if r["parent"] is None}
            if all(r["status"] in STEP_DONE for r in rows.values()):
                self._finish(rid, "failed" if any(r["status"] == "failed" for r in rows.values()) else "succeeded")
                return
            if not futures:                              # what is pending now waits on steps that wait
                due = [r["next_ms"] for r in rows.values() if r["status"] in ("waiting", "retrying") and r["next_ms"]]
                nxt = min(due) if due else None
                if nxt is None or nxt - _now() > 2000:
                    self._release(rid, status="waiting", wake_ms=nxt)   # parked: a tick (or a decision) resumes it
                    return
            ev = self._driving.get(rid)
            if futures:
                wait([f for f, _ in futures.values()], timeout=0.5, return_when=FIRST_COMPLETED)
            elif ev is not None:
                ev.wait(0.3)
                ev.clear()
            else:
                time.sleep(0.3)

    def _skip_reason(self, s: dict, deps: list[dict], by: dict, rows: dict) -> Optional[str]:
        for d in deps:                                    # a branch that chose the other side
            src = by[d["key"]]
            if src["type"] == "branch" and d["status"] == "ok":
                chosen = (json.loads(d["output"] or "{}") or {}).get("chosen")
                side = "then" if s["name"] in src["then"] else "else" if s["name"] in src["else"] else None
                if side and side != chosen:
                    return f"not chosen by {src['name']} ({src['name']} took {chosen})"
        statuses = [d["status"] for d in deps]
        when = s["when"]
        if when == "all_ok" and any(x != "ok" for x in statuses):
            bad = next(d["key"] for d in deps if d["status"] != "ok")
            return f"{bad} did not succeed"
        if when == "none_failed" and any(x in ("failed", "cancelled") for x in statuses):
            bad = next(d["key"] for d in deps if d["status"] in ("failed", "cancelled"))
            return f"{bad} failed"
        if when == "any_failed" and not any(x == "failed" for x in statuses):
            return "nothing before it failed"
        return None

    def _submit(self, rid: str, run: dict, defn: dict, s: dict, r: dict, values: dict, cancel: threading.Event,
                resume: bool = False) -> tuple[Future, stepmod.Ctx]:
        now = _now()
        attempt = r["attempt"] if resume else r["attempt"] + 1
        started = r["started_ms"] if resume and r["started_ms"] else now
        step_cancel = threading.Event()
        ctx = stepmod.Ctx(owner=run["run_as"], flow=defn["name"], run_id=rid, step=s, attempt=attempt, values=values,
                          started_ms=started, deadline_ms=started + s["timeout_s"] * 1000, timezone=self._tz(defn),
                          state=dict(r["state"] or {}) if resume else {}, decision=r["decision"],
                          cancelled=_Either(cancel, step_cancel))
        ctx.step_cancel = step_cancel  # type: ignore[attr-defined]
        values_now = {"status": "running", "next_ms": None}
        if not resume:
            values_now.update(attempt=attempt, started_ms=now, finished_ms=None, error=None)
        self._set_step(rid, s["name"], **values_now)
        return self.pool.submit(self._execute, ctx), ctx

    def _execute(self, ctx: stepmod.Ctx) -> Any:
        s = ctx.step
        if s["type"] == "for_each":
            return self._for_each(ctx)
        if s["type"] not in ("branch",):
            keep = {"condition", "then", "else"}
            try:
                ctx.step = {k: (v if k in keep or k in model.COMMON else expr.render(v, ctx.values)) for k, v in s.items()}
            except expr.ExprError as e:
                raise stepmod.StepFailed(f"a template: {e}")
        return stepmod.EXECUTORS[s["type"]](self.b, ctx)

    def _for_each(self, ctx: stepmod.Ctx) -> stepmod.Done:
        s = ctx.step
        raw = s["items"]
        try:
            if isinstance(raw, str):
                inner = expr.TEMPLATE.fullmatch(raw.strip())
                items = expr.evaluate(inner.group(1) if inner else raw, ctx.values)
            else:
                items = expr.render(raw, ctx.values)
        except expr.ExprError as e:
            raise stepmod.StepFailed(f"items: {e}")
        if not isinstance(items, list):
            raise stepmod.StepFailed(f"items gave {type(items).__name__}, not a list")
        if len(items) > 1000:
            raise stepmod.StepFailed(f"items gave {len(items)} items; a for-each takes at most 1000")
        rid, name = ctx.run_id, s["name"]
        with self.engine.begin() as conn:
            conn.execute(delete(flow_step_runs).where(and_(flow_step_runs.c.run_id == rid, flow_step_runs.c.parent == name)))
            if items:
                conn.execute(insert(flow_step_runs), [
                    {"run_id": rid, "key": f"{name}[{i}]", "step": name, "parent": name, "type": s["step"]["type"],
                     "status": "pending", "attempt": 0, "started_ms": None, "finished_ms": None, "next_ms": None,
                     "output": None, "log": None, "error": None, "state": {"item": item}, "decision": None}
                    for i, item in enumerate(items)])
        results: list[Any] = [None] * len(items)
        errors: dict[int, str] = {}

        def one(i: int, item: Any) -> None:
            key = f"{name}[{i}]"
            child = {**s["step"], "name": key, "after": [], "when": "all_ok"}
            values = {**ctx.values, "item": item, "index": i}
            for attempt in range(1, child["retries"] + 2):
                if ctx.is_cancelled():
                    self._set_step(rid, key, status="cancelled", finished_ms=_now(), error="cancelled")
                    errors[i] = "cancelled"
                    return
                self._set_step(rid, key, status="running", attempt=attempt, started_ms=_now())
                c = stepmod.Ctx(owner=ctx.owner, flow=ctx.flow, run_id=rid, step=child, attempt=attempt, values=values,
                                started_ms=_now(), deadline_ms=min(ctx.deadline_ms, _now() + child["timeout_s"] * 1000),
                                timezone=ctx.timezone, cancelled=ctx.cancelled)
                try:
                    c.step = {k: (v if k in model.COMMON else expr.render(v, values)) for k, v in child.items()}
                    res = stepmod.EXECUTORS[child["type"]](self.b, c)
                    while isinstance(res, stepmod.Wait):          # an Airflow DAG run inside a for-each: look again
                        if ctx.is_cancelled() or _now() > c.deadline_ms:
                            raise stepmod.StepFailed("timed out" if not ctx.is_cancelled() else "cancelled")
                        c.state = res.state
                        time.sleep(max(0.2, min((res.next_ms - _now()) / 1000, 30)))
                        res = stepmod.EXECUTORS[child["type"]](self.b, c)
                    results[i] = res.output
                    self._set_step(rid, key, status="ok", finished_ms=_now(), output=res.output,
                                   log="\n".join(c.lines + ([res.log] if res.log else [])))
                    errors.pop(i, None)
                    return
                except Exception as e:  # noqa: BLE001 - this item failed; it may be retried
                    msg = str(e) or type(e).__name__
                    errors[i] = msg
                    last = attempt >= child["retries"] + 1
                    self._set_step(rid, key, status="failed" if last else "retrying", error=msg,
                                   finished_ms=_now() if last else None, log="\n".join(c.lines))
                    if not last:
                        time.sleep(min(child["retry_delay_s"] * child["retry_backoff"] ** (attempt - 1), 300))

        with ThreadPoolExecutor(max_workers=s["concurrency"], thread_name_prefix="cairn-flow-item") as pool:
            for f in [pool.submit(one, i, item) for i, item in enumerate(items)]:
                f.result()
        ok = len(items) - len(errors)
        ctx.log(f"{ok} of {len(items)} items succeeded")
        if errors:
            i = min(errors)
            raise stepmod.StepFailed(f"{len(errors)} of {len(items)} items failed; item {i}: {errors[i]}")
        return stepmod.Done({"items": len(items), "succeeded": ok, "results": results})

    def _apply(self, rid: str, s: dict, r: dict, ctx: stepmod.Ctx, fut: Future) -> None:
        now = _now()
        try:
            res = fut.result()
        except stepmod.StepFailed as e:
            self._failed(rid, s, r, str(e), ctx.lines)
            return
        except Exception as e:  # noqa: BLE001 - a step's own failure; never the driver's
            logger.exception("flow run %s: step %s raised", rid, s["name"])
            self._failed(rid, s, r, f"{type(e).__name__}: {e}", ctx.lines)
            return
        log = "\n".join(ctx.lines + ([res.log] if getattr(res, "log", "") else []))
        if isinstance(res, stepmod.Wait):
            self._set_step(rid, s["name"], status="waiting", next_ms=res.next_ms, state=res.state,
                           log=((r["log"] + "\n") if r["log"] else "") + log if log else r["log"])
            return
        self._set_step(rid, s["name"], status="ok", finished_ms=now, output=res.output, error=None, next_ms=None,
                       log=((r["log"] + "\n") if r["log"] and s["type"] in stepmod.WAITING else "") + log)
        if s["type"] == "branch":                        # the other side is skipped now, so it shows at once
            chosen = res.output.get("chosen")
            for n in s["else" if chosen == "then" else "then"]:
                with self.engine.begin() as conn:
                    conn.execute(update(flow_step_runs).where(and_(
                        flow_step_runs.c.run_id == rid, flow_step_runs.c.key == n, flow_step_runs.c.status == "pending"))
                        .values(status="skipped", finished_ms=now, error=f"not chosen by {s['name']} ({s['name']} took {chosen})"))
        self._emit_lineage(rid, ctx, s, "COMPLETE", r["started_ms"] or now, res.output)

    def _failed(self, rid: str, s: dict, r: dict, error: str, lines: list) -> None:
        now = _now()
        attempt = r["attempt"] or 1
        log = "\n".join(lines) if lines else None
        if attempt <= s["retries"] and "rejected by" not in error:
            delay = min(s["retry_delay_s"] * (s["retry_backoff"] ** (attempt - 1)), 3600)
            self._set_step(rid, s["name"], status="retrying", next_ms=now + int(delay * 1000), error=error,
                           state=None, **({"log": log} if log else {}))
            return
        self._set_step(rid, s["name"], status="failed", finished_ms=now, error=error, next_ms=None,
                       **({"log": log} if log else {}))
        self._emit_lineage(rid, None, s, "FAIL", r["started_ms"] or now, None, error)

    def _finish(self, rid: str, status: str) -> None:
        now = _now()
        with self.engine.begin() as conn:
            done = conn.execute(update(flow_runs).where(and_(flow_runs.c.id == rid, flow_runs.c.status.in_(ACTIVE)))
                                .values(status=status, finished_ms=now, lease_owner=None, lease_until_ms=None,
                                        wake_ms=None)).rowcount
        if not done:
            return
        run = self._run_row(rid)
        try:
            row = self._row(run["flow_id"])
        except FlowError:
            return
        defn = json.loads(run["definition"])
        rows = [r for r in self._step_rows(rid) if r["parent"] is None]
        failed = [r for r in rows if r["status"] == "failed"]
        self._audit(run["triggered_by"] or row["owner"], f"flow.run_{status}", row["name"],
                    {"run": rid, "failed_steps": [r["key"] for r in failed]})
        lines = [f"{r['key']}: {r['status']}" + (f" ({r['error']})" if r["error"] and r["status"] != "ok" else "")
                 for r in sorted(rows, key=lambda r: r["started_ms"] or 0)]
        if status in ("failed", "succeeded"):
            self._tell(row, defn, rid, status, "\n".join(lines))
        self._after(row, status)
        self._admit(row["id"])

    def _tell(self, row: dict, defn: dict, rid: str, status: str, body: str) -> None:
        on = (defn.get("notify") or {}).get("on", ["failure", "sla"])
        want = {"failed": "failure", "succeeded": "success", "sla": "sla"}[status]
        if self.notify is None or want not in on:
            return
        who = [row["owner"], *[x for x in (defn.get("notify") or {}).get("to", []) if x != row["owner"]]]
        kind = {"failed": "flow.failed", "succeeded": "flow.succeeded", "sla": "flow.sla_missed"}[status]
        title = {"failed": f"Flow {row['name']} failed", "succeeded": f"Flow {row['name']} succeeded",
                 "sla": f"Flow {row['name']} is late"}[status]
        try:
            self.notify(kind, title, body, who, {"flow": row["name"], "run": rid, "status": status})
        except Exception:  # noqa: BLE001 - the run is recorded either way
            logger.exception("flow %s: could not tell about run %s", row["name"], rid)

    def _after(self, row: dict, status: str) -> None:
        """Flows that start after this one, on this outcome."""
        event = {"succeeded": "succeeded", "failed": "failed"}.get(status)
        if event is None:
            return
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(flows).where(and_(
                flows.c.enabled.is_(True), flows.c.published_version.is_not(None))))]
        for other in rows:
            for f in other["follows"] or []:
                if f["flow"] == row["name"] and (event in f["on"] or "finished" in f["on"]):
                    try:
                        defn = self._version(other["id"], other["published_version"])
                        self._new_run(other, other["published_version"], defn, "flow", None, None,
                                      detail={"after": {"flow": row["name"], "status": status}})
                        self._admit(other["id"])
                    except Exception:  # noqa: BLE001 - one follower never stops the others
                        logger.exception("flow %s: could not start after %s", other["name"], row["name"])
                    break

    def _emit_lineage(self, rid: str, ctx: Optional[stepmod.Ctx], s: dict, state: str, started_ms: int,
                      output: Optional[dict], error: Optional[str] = None) -> None:
        """Steps that read or write tables (declared ``reads``/``writes``; a derived table's target) as OpenLineage
        runs of the job ``cairn-flows://<flow>``/``<step>``: lineage in the catalog, and changes for what they wrote."""
        if self.lineage is None:
            return
        writes = list(s.get("writes") or [])
        if s["type"] == "derived" and output and output.get("target"):
            writes.append(output["target"])
        reads = list(s.get("reads") or [])
        if not reads and not writes:
            return
        try:
            flow_name = json.loads(self._run_row(rid)["definition"])["name"]

            def ds(t: str) -> dict:
                urn = self.table_urn(t)
                inner = urn[len("urn:li:dataset:("):-1]
                platform, rest = inner.split(",", 1)
                name = rest.rsplit(",", 1)[0]
                return {"namespace": f"{platform.removeprefix('urn:li:dataPlatform:')}://", "name": name}
            job = {"namespace": f"{LINEAGE_NS}://{flow_name}", "name": s["name"]}
            run = {"runId": f"{rid}-{s['name']}"}
            iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()  # noqa: E731
            base = {"job": job, "run": run, "inputs": [ds(t) for t in reads], "outputs": [ds(t) for t in writes],
                    "producer": "https://cairn/flows", "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json"}
            self.lineage({**base, "eventType": "START", "eventTime": iso(started_ms)})
            end = {**base, "eventType": state, "eventTime": iso(_now())}
            if error:
                end["run"] = {**run, "facets": {"errorMessage": {"message": error[:500], "programmingLanguage": "SQL"}}}
            self.lineage(end)
        except Exception:  # noqa: BLE001 - lineage never fails a step
            logger.exception("flow run %s: lineage for %s failed", rid, s["name"])

    # ---- decisions, cancels ------------------------------------------------------------------------------------
    def decide(self, actor: str, rid: str, step: str, decision: str, comment: str = "") -> dict:
        if decision not in ("approve", "reject"):
            raise FlowError("decision: approve or reject")
        run = self._run_row(rid)
        row = self._row(run["flow_id"])
        defn = json.loads(run["definition"])
        s = next((x for x in defn["steps"] if x["name"] == step and x["type"] == "approval"), None)
        if s is None:
            raise FlowError(f"no approval step {step!r} in that run", 404)
        groups = {str(g).removeprefix("urn:li:corpGroup:") for g in self._groups(actor)}
        if actor not in s["approvers"] and not (groups & set(s["groups"])):
            raise FlowError(f"you are not one of the approvers of {step}", 403)
        d = {"decision": decision, "by": actor, "comment": str(comment or "")[:2000], "at_ms": _now()}
        with self.engine.begin() as conn:
            n = conn.execute(update(flow_step_runs).where(and_(
                flow_step_runs.c.run_id == rid, flow_step_runs.c.key == step, flow_step_runs.c.decision.is_(None),
                flow_step_runs.c.status.in_(("waiting", "running")))).values(decision=d, next_ms=_now())).rowcount
            if n:
                conn.execute(update(flow_runs).where(and_(flow_runs.c.id == rid, flow_runs.c.status == "waiting"))
                             .values(wake_ms=_now()))
        if not n:
            raise FlowError(f"{step} is not waiting for a decision", 409)
        self._audit(actor, f"flow.approval_{decision}d" if decision == "approve" else "flow.approval_rejected",
                    row["name"], {"run": rid, "step": step, "comment": d["comment"]})
        self.kick(rid)
        return self._run_public(rid)

    def cancel(self, actor: str, rid: str) -> dict:
        run = self._run_row(rid)
        row = self._row(run["flow_id"])
        self._need(actor, row, "run")
        if run["status"] not in ACTIVE:
            raise FlowError("the run has finished", 409)
        with self.engine.begin() as conn:
            conn.execute(update(flow_runs).where(flow_runs.c.id == rid).values(cancel_requested=True, wake_ms=_now()))
            n = conn.execute(update(flow_runs).where(and_(flow_runs.c.id == rid, flow_runs.c.status == "queued"))
                             .values(status="cancelled", finished_ms=_now())).rowcount
            if n:
                conn.execute(update(flow_step_runs).where(flow_step_runs.c.run_id == rid)
                             .values(status="cancelled", error="the run was cancelled"))
        self._audit(actor, "flow.run_cancel_requested", row["name"], {"run": rid})
        if not n:
            self.kick(rid)                               # the driver (here or where it runs) sees the request
        return self._run_public(rid)

    # ---- reading runs ------------------------------------------------------------------------------------------
    def _run_public(self, rid: str, full: bool = False) -> dict:
        run = self._run_row(rid)
        defn = json.loads(run["definition"])
        rows = self._step_rows(rid)
        order_ = {s["name"]: i for i, s in enumerate(defn["steps"])}
        top = sorted((r for r in rows if r["parent"] is None), key=lambda r: order_.get(r["key"], 0))
        kids: dict[str, list[dict]] = {}
        for r in rows:
            if r["parent"] is not None:
                kids.setdefault(r["parent"], []).append(r)

        def pub(r: dict) -> dict:
            out = {"key": r["key"], "step": r["step"], "type": r["type"], "status": r["status"], "attempt": r["attempt"],
                   "started_ms": r["started_ms"], "finished_ms": r["finished_ms"], "next_ms": r["next_ms"],
                   "error": r["error"], "output": json.loads(r["output"]) if r["output"] else None,
                   "reused": bool((r["state"] or {}).get("reused_from")), "decision": r["decision"]}
            if full:
                out["log"] = r["log"]
            if r["parent"] is None and r["key"] in kids:
                out["items"] = [pub(k) for k in sorted(kids[r["key"]], key=lambda k: int(k["key"].rsplit("[", 1)[1][:-1]))]
            if r["type"] == "approval" and r["status"] in ("waiting", "running"):
                s = next((x for x in defn["steps"] if x["name"] == r["key"]), {})
                out["approvers"], out["groups"] = s.get("approvers", []), s.get("groups", [])
            return out
        out = {"id": run["id"], "flow_id": run["flow_id"], "flow": defn["name"], "version": run["version"],
               "status": run["status"], "trigger": run["trigger"], "triggered_by": run["triggered_by"],
               "run_as": run["run_as"], "params": run["params"], "logical_date": run["logical_date"],
               "detail": run["detail"], "created_ms": run["created_ms"], "started_ms": run["started_ms"],
               "finished_ms": run["finished_ms"], "error": run["error"], "cancel_requested": bool(run["cancel_requested"]),
               "steps": [pub(r) for r in top]}
        if full:
            out["definition"] = defn
        return out

    def runs(self, actor: str, ref: str, limit: int = 50) -> list[dict]:
        row = self._row(ref)
        self._need(actor, row, "view")
        with self.engine.connect() as conn:
            rows = conn.execute(select(flow_runs.c.id, flow_runs.c.version, flow_runs.c.status, flow_runs.c.trigger,
                                       flow_runs.c.triggered_by, flow_runs.c.created_ms, flow_runs.c.started_ms,
                                       flow_runs.c.finished_ms, flow_runs.c.logical_date, flow_runs.c.error)
                                .where(flow_runs.c.flow_id == row["id"]).order_by(flow_runs.c.created_ms.desc())
                                .limit(max(1, min(limit, 500)))).all()
        return [dict(r._mapping) for r in rows]

    def run_detail(self, actor: str, rid: str) -> dict:
        run = self._run_row(rid)
        self._need(actor, self._row(run["flow_id"]), "view")
        return self._run_public(rid, full=True)

    def step_log(self, actor: str, rid: str, key: str) -> dict:
        run = self._run_row(rid)
        self._need(actor, self._row(run["flow_id"]), "view")
        r = next((x for x in self._step_rows(rid) if x["key"] == key), None)
        if r is None:
            raise FlowError("no such step in that run", 404)
        return {"key": key, "status": r["status"], "log": r["log"] or "", "error": r["error"],
                "output": json.loads(r["output"]) if r["output"] else None}

    def pending_approvals(self, actor: str) -> list[dict]:
        """Approval steps waiting for ``actor`` (as a named approver or through a group)."""
        groups = {str(g).removeprefix("urn:li:corpGroup:") for g in self._groups(actor)}
        with self.engine.connect() as conn:
            rows = conn.execute(select(flow_step_runs.c.run_id, flow_step_runs.c.key, flow_runs.c.definition,
                                       flow_step_runs.c.started_ms)
                                .join(flow_runs, flow_runs.c.id == flow_step_runs.c.run_id)
                                .where(and_(flow_step_runs.c.type == "approval", flow_step_runs.c.decision.is_(None),
                                            flow_step_runs.c.status.in_(("waiting", "running"))))).all()
        out = []
        for r in rows:
            defn = json.loads(r.definition)
            s = next((x for x in defn["steps"] if x["name"] == r.key), None)
            if s and (actor in s["approvers"] or groups & set(s["groups"])):
                out.append({"run": r.run_id, "flow": defn["name"], "step": r.key, "message": s.get("message") or "",
                            "since_ms": r.started_ms})
        return out

    # ---- the scheduler -----------------------------------------------------------------------------------------
    def run_due(self, now: Optional[int] = None) -> int:
        """What the scheduler's tick does: scheduled and dataset starts (each claimed, so several replicas start each
        run once), admission, runs to resume (parked until now, or whose driver's lease lapsed), SLAs, retention."""
        now = now or _now()
        started = 0
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(flows).where(and_(
                flows.c.enabled.is_(True), flows.c.published_version.is_not(None), flows.c.next_run_ms.is_not(None),
                flows.c.next_run_ms <= now)))]
        for row in due:
            try:
                defn = self._version(row["id"], row["published_version"])
                sched = next((t for t in defn["triggers"] if t["type"] == "schedule" and t["enabled"]), None)
                nxt = next_fire(sched["cron"], sched["timezone"], max(now, row["next_run_ms"])) if sched else None
                with self.engine.begin() as conn:            # claim it: another replica skips it
                    got = conn.execute(update(flows).where(and_(flows.c.id == row["id"],
                                                                flows.c.next_run_ms == row["next_run_ms"]))
                                       .values(next_run_ms=nxt)).rowcount
                if not got or sched is None:
                    continue
                from zoneinfo import ZoneInfo

                logical = datetime.fromtimestamp(row["next_run_ms"] / 1000, tz=ZoneInfo(sched["timezone"])).date().isoformat()
                self._new_run(row, row["published_version"], defn, "schedule", None, None, logical,
                              {"scheduled_ms": row["next_run_ms"]})
                started += 1
            except Exception:  # noqa: BLE001 - one flow never stops the others
                logger.exception("flow %s: scheduled start failed", row["name"])
        started += self._dataset_starts()
        with self.engine.connect() as conn:
            queued = {r[0] for r in conn.execute(select(flow_runs.c.flow_id).where(flow_runs.c.status == "queued").distinct())}
        for fid in queued:
            self._admit(fid)
        with self.engine.connect() as conn:
            resume = [r[0] for r in conn.execute(select(flow_runs.c.id).where(or_(
                and_(flow_runs.c.status == "running", or_(flow_runs.c.lease_owner.is_(None), flow_runs.c.lease_until_ms < now)),
                and_(flow_runs.c.status == "waiting", flow_runs.c.wake_ms <= now,
                     or_(flow_runs.c.lease_owner.is_(None), flow_runs.c.lease_until_ms < now)))))]
        for rid in resume:
            self.kick(rid)
        self._slas(now)
        if now - self._last_sweep > 3_600_000:
            self._last_sweep = now
            self._retention(now)
        return started

    def _dataset_starts(self) -> int:
        if self.changes is None:
            return 0
        n = 0
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(flows).where(and_(
                flows.c.enabled.is_(True), flows.c.published_version.is_not(None))))]
        for row in rows:
            if not row["watch"]:
                continue
            try:
                own = f"urn:li:dataJob:(urn:li:dataFlow:({LINEAGE_NS},{row['name']},"
                seen = row["seen_change"]
                changed = [c for c in self.changes.since(row["watch"], seen, 500)]
                if not changed:
                    continue
                head = max(c["seq"] for c in changed)
                theirs = [c for c in changed if not str(c.get("job") or "").startswith(own)]
                with self.engine.begin() as conn:            # claim the changes: another replica skips them
                    got = conn.execute(update(flows).where(and_(flows.c.id == row["id"], flows.c.seen_change == seen))
                                       .values(seen_change=head)).rowcount
                if not got or not theirs:
                    continue
                defn = self._version(row["id"], row["published_version"])
                self._new_run(row, row["published_version"], defn, "dataset", None, None,
                              detail={"tables": sorted({c["urn"] for c in theirs})[:20],
                                      "jobs": sorted({c["job"] for c in theirs if c.get("job")})[:20]})
                n += 1
            except Exception:  # noqa: BLE001 - one flow never stops the others
                logger.exception("flow %s: dataset start failed", row["name"])
        return n

    def _slas(self, now: int) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(select(flow_runs.c.id, flow_runs.c.flow_id, flow_runs.c.definition, flow_runs.c.created_ms)
                                .where(and_(flow_runs.c.status.in_(ACTIVE), flow_runs.c.sla_alerted.is_(False)))).all()
        for r in rows:
            defn = json.loads(r.definition)
            sla = defn.get("sla_minutes")
            if not sla or r.created_ms + sla * 60_000 > now:
                continue
            with self.engine.begin() as conn:
                got = conn.execute(update(flow_runs).where(and_(flow_runs.c.id == r.id, flow_runs.c.sla_alerted.is_(False)))
                                   .values(sla_alerted=True)).rowcount
            if got:
                try:
                    row = self._row(r.flow_id)
                except FlowError:
                    continue
                self._audit(row["owner"], "flow.sla_missed", row["name"], {"run": r.id, "sla_minutes": sla})
                self._tell(row, defn, r.id, "sla", f"The run started {sla} minutes ago or more and has not finished.")

    def _retention(self, now: int) -> None:
        cutoff = now - self.keep_days * 86_400_000
        with self.engine.begin() as conn:
            old = [r[0] for r in conn.execute(select(flow_runs.c.id).where(and_(
                flow_runs.c.status.in_(DONE), flow_runs.c.created_ms < cutoff)).limit(5000))]
            for i in range(0, len(old), 500):
                conn.execute(delete(flow_step_runs).where(flow_step_runs.c.run_id.in_(old[i:i + 500])))
                conn.execute(delete(flow_runs).where(flow_runs.c.id.in_(old[i:i + 500])))

    # ---- SQL workflows as flows ------------------------------------------------------------------------------
    @staticmethod
    def from_sql_workflow(wf: dict) -> dict:
        """A draft flow from a SQL workflow (``/v1/sql-workflows``): the same steps, schedule and recipients."""
        import re

        steps = []
        for s in wf["steps"]:
            base = {"name": re.sub(r"[^A-Za-z0-9_-]", "-", s["name"])[:63] or "step", "after": s.get("after") or [],
                    "retries": s.get("retries", 0)}
            if not base["name"][0].isalpha():
                base["name"] = "s-" + base["name"][:60]
            if s["type"] == "derived":
                steps.append({**base, "type": "derived", "table": s["table"], "full": bool(s.get("full"))})
            elif s["type"] == "check":
                src = {"query": s["query"]} if s.get("query") else {"sql": s["sql"], "connection": s["connection"]}
                steps.append({**base, "type": "check", **src, "measure": s.get("measure", "rows"), "op": s["op"],
                              "value": s["value"]})
            else:
                src = {"query": s["query"]} if s.get("query") else {"sql": s["sql"], "connection": s["connection"]}
                steps.append({**base, "type": "sql", **src})
        name = re.sub(r"[^a-z0-9_-]", "-", wf["name"].lower()).strip("-")[:63] or "workflow"
        if not name[0].isalpha():
            name = "wf-" + name[:59]
        out: dict[str, Any] = {"kind": "flow", "name": name, "description": f"From the SQL workflow {wf['name']}",
                               "steps": steps,
                               "notify": {"on": ["failure", "success"] if wf.get("notify") == "always" else ["failure"],
                                          "to": [r for r in wf.get("recipients") or [] if r.startswith("urn:li:corpuser:")]}}
        if wf.get("cron"):
            out["triggers"] = [{"type": "schedule", "cron": wf["cron"], "timezone": "UTC"}]
        return out


class _Either:
    """Set when either event is (the run cancelled, or this step ran out of time)."""

    def __init__(self, a: threading.Event, b: threading.Event):
        self.a, self.b = a, b

    def is_set(self) -> bool:
        return self.a.is_set() or self.b.is_set()

    def set(self) -> None:
        self.b.set()
