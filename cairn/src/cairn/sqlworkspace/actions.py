"""App actions: forms and buttons on a dashboard that change things, as the person who clicks.

A dashboard's owner adds actions (``POST /sql/dashboards/<id>/actions``); anyone who may open the dashboard sees
them and runs them (``POST …/actions/<action>/run`` with ``inputs`` from the form and, for a row action, the
``row`` clicked). Four kinds:

* ``input_row``: a form that adds a row to an input table (:mod:`cairn.sqlworkspace.input_tables`). ``fields``
  are the input table's columns the form asks for, ``constants`` fixed values. Written on Trino **as the person
  clicking**, so Cairn's policies decide (``WRITE_DATASET``); a person who may not write is refused.
* ``input_update``: a button that changes the clicked row of an input table: ``set`` (fixed values, such as
  ``{"status": "approved"}``) and optional ``fields``. Written as the person, with the row's ``_updated_at`` as
  read, so a change made since is refused rather than overwritten.
* ``workflow``: runs a SQL workflow (:mod:`cairn.sqlworkspace.workflows`). A workflow runs as its owner, so only
  its owner (or an operator) may put it on a dashboard, and ``who`` says who may click it: ``owner`` (default) or
  ``viewers``. The person clicking sees each step's status, never its rows.
* ``webhook``: posts the form (and who sent it) to an API. Only an operator adds one, and the URL must start with
  an origin in ``CAIRN_ACTION_WEBHOOK_ORIGINS`` (comma separated, such as ``https://hooks.example.com``);
  redirects are not followed. With ``CAIRN_ACTION_WEBHOOK_SECRET`` the body is signed
  (``X-Cairn-Signature: sha256=<hex HMAC of the body>``).

Inputs are only the fields an action declares, typed by the input table's columns (or as ``text``, ``number``,
``integer``, ``boolean``, ``date`` for webhooks). Each run is kept (``GET …/actions/<action>/runs``, the dashboard's
owner) with who ran it, the outcome and the time, not the values, and audited as ``bi.action_run``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, insert, select

from cairn.common.db import metadata
from cairn.sqlworkspace.input_tables import TYPES, InputTableError, typed_literal
from cairn.sqlworkspace.workspace import WorkspaceError

KINDS = ("input_row", "input_update", "workflow", "webhook")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MAX_ACTIONS = 20

bi_actions = Table(
    "cairn_bi_actions", metadata,
    Column("id", String(32), primary_key=True),
    Column("dashboard", String(32), nullable=False, index=True),
    Column("name", String(64), nullable=False),
    Column("label", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("config", Text, nullable=False),
    Column("owner", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
bi_action_runs = Table(
    "cairn_bi_action_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("action", String(32), nullable=False, index=True),
    Column("actor", String(256), nullable=False),
    Column("status", String(16), nullable=False),          # ok | refused | error
    Column("detail", String(500), nullable=False, default=""),
    Column("ms", Integer, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


class Actions:
    def __init__(self, engine, boards, input_tables=None, workflows=None, audit=None,
                 is_operator: Optional[Callable[[str], bool]] = None, post: Optional[Callable[..., Any]] = None):
        """``post(url, body: bytes, headers)``: sends a webhook (tests and the default use httpx, no redirects)."""
        self.engine, self.boards, self.it, self.wf, self.audit = engine, boards, input_tables, workflows, audit
        self.is_operator = is_operator or (lambda actor: False)
        self.post = post or self._post

    # ---- definitions --------------------------------------------------------------------------
    def _origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in os.environ.get("CAIRN_ACTION_WEBHOOK_ORIGINS", "").split(",") if o.strip()]

    def _fields(self, raw: Any, kinds: dict[str, str]) -> list[dict]:
        if not isinstance(raw, list) or len(raw) > 30:
            raise ActionError("fields: a list (up to 30)")
        out = []
        for f in raw:
            f = {"name": f} if isinstance(f, str) else f
            if not isinstance(f, dict) or not isinstance(f.get("name"), str):
                raise ActionError("a field is a column name or {name, label, type, required}")
            name = f["name"]
            kind = kinds.get(name) if kinds else f.get("type", "text")
            if kinds and kind is None:
                raise ActionError(f"field {name}: not a column of the input table")
            if kind not in TYPES:
                raise ActionError(f"field {name}: type is one of {', '.join(TYPES)}")
            out.append({"name": name, "label": str(f.get("label") or name)[:120], "type": kind,
                        "required": bool(f.get("required"))})
        return out

    def _config(self, actor: str, kind: str, body: dict) -> dict:
        if kind in ("input_row", "input_update"):
            if self.it is None:
                raise ActionError("input tables are not available here")
            try:
                t = self.it._get(str(body.get("input_table") or ""))
            except InputTableError as e:
                raise ActionError(f"input_table: {e}", e.status)
            kinds = {c["name"]: c["type"] for c in t["columns"]}
            fields = self._fields(body.get("fields") or [], kinds)
            fixed = body.get("constants" if kind == "input_row" else "set") or {}
            if not isinstance(fixed, dict) or set(fixed) - set(kinds):
                raise ActionError(f"{'constants' if kind == 'input_row' else 'set'}: columns of the input table")
            for k, v in fixed.items():
                typed_literal(v, kinds[k], k)                      # a value of the column's type
            if kind == "input_update" and not fixed and not fields:
                raise ActionError("an input_update sets values or asks for fields")
            return {"input_table": t["id"], "table": t["table_name"], "fields": fields,
                    ("constants" if kind == "input_row" else "set"): fixed}
        if kind == "workflow":
            if self.wf is None:
                raise ActionError("SQL workflows are not available here")
            wf = self.wf.get(actor, str(body.get("workflow") or ""))      # its owner or an operator: else 404
            if wf["owner"] != actor and not self.is_operator(actor):
                raise ActionError("only a workflow's owner puts it on a dashboard", 403)
            who = body.get("who", "owner")
            if who not in ("owner", "viewers"):
                raise ActionError("who: owner or viewers")
            return {"workflow": wf["id"], "workflow_owner": wf["owner"], "who": who}
        if kind == "webhook":
            if not self.is_operator(actor):
                raise ActionError("a webhook action sends data out of Cairn: an operator adds it", 403)
            url = str(body.get("url") or "")
            origins = self._origins()
            if not any(url == o or url.startswith(o + "/") for o in origins):
                raise ActionError("url: must start with an origin in CAIRN_ACTION_WEBHOOK_ORIGINS"
                                  + ("" if origins else " (none configured)"), 403)
            return {"url": url, "fields": self._fields(body.get("fields") or [], {})}
        raise ActionError(f"kind: one of {', '.join(KINDS)}")

    def add(self, actor: str, did: str, body: dict) -> dict:
        board = self.boards.dashboard(actor, did)
        if board["owner"] != actor:
            raise ActionError("only the dashboard's owner adds actions", 403)
        if not isinstance(body, dict):
            raise ActionError("an action is an object")
        name, kind = str(body.get("name") or ""), str(body.get("kind") or "")
        if not NAME.match(name):
            raise ActionError("name: lowercase letters, digits and _")
        config = self._config(actor, kind, body)
        config["confirm"] = str(body.get("confirm") or "")[:200]
        with self.engine.begin() as conn:
            rows = conn.execute(select(bi_actions.c.name).where(bi_actions.c.dashboard == did)).all()
            if name in {r[0] for r in rows}:
                raise ActionError(f"an action {name} exists on this dashboard", 409)
            if len(rows) >= MAX_ACTIONS:
                raise ActionError(f"at most {MAX_ACTIONS} actions on a dashboard")
            aid = uuid.uuid4().hex
            conn.execute(insert(bi_actions).values(id=aid, dashboard=did, name=name, kind=kind, owner=actor,
                                                   label=str(body.get("label") or name)[:200],
                                                   config=json.dumps(config), created_ms=_now()))
        self._audit(actor, "bi.action_added", did, {"action": name, "kind": kind})
        return self._get(did, aid)

    def _get(self, did: str, aid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(bi_actions).where((bi_actions.c.id == aid) & (bi_actions.c.dashboard == did))).first()
        if r is None:
            raise ActionError("no such action", 404)
        return self._public(r)

    @staticmethod
    def _public(r) -> dict:
        d = dict(r._mapping)
        config = json.loads(d.pop("config"))
        config.pop("url", None)                                   # where a webhook goes is the operator's business
        return {**d, "config": config}

    def list(self, actor: str, did: str) -> list[dict]:
        self.boards.dashboard(actor, did)                         # those who may open the dashboard
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(select(bi_actions).where(bi_actions.c.dashboard == did)
                                                          .order_by(bi_actions.c.created_ms))]

    def remove(self, actor: str, did: str, aid: str) -> None:
        board = self.boards.dashboard(actor, did)
        if board["owner"] != actor and not self.is_operator(actor):
            raise ActionError("only the dashboard's owner removes actions", 403)
        with self.engine.begin() as conn:
            if not conn.execute(delete(bi_actions).where((bi_actions.c.id == aid) & (bi_actions.c.dashboard == did))).rowcount:
                raise ActionError("no such action", 404)
        self._audit(actor, "bi.action_removed", did, {"action": aid})

    def runs(self, actor: str, did: str, aid: str, limit: int = 50) -> list[dict]:
        board = self.boards.dashboard(actor, did)
        if board["owner"] != actor and not self.is_operator(actor):
            raise ActionError("only the dashboard's owner sees an action's runs", 403)
        self._get(did, aid)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_action_runs).where(bi_action_runs.c.action == aid)
                                .order_by(bi_action_runs.c.created_ms.desc()).limit(max(1, min(limit, 500)))).all()
        return [dict(r._mapping) for r in rows]

    # ---- running ----------------------------------------------------------------------------
    @staticmethod
    def _inputs(fields: list[dict], inputs: Any) -> dict:
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise ActionError("inputs: an object of the action's fields")
        names = {f["name"] for f in fields}
        extra = set(inputs) - names
        if extra:
            raise ActionError(f"inputs: {sorted(extra)[0]} is not a field of this action")
        for f in fields:
            v = inputs.get(f["name"])
            if f["required"] and (v is None or v == ""):
                raise ActionError(f"{f['label']}: required")
            if v is not None and v != "":
                try:
                    typed_literal(v, f["type"], f["label"])
                except InputTableError as e:
                    raise ActionError(str(e))
        return {k: v for k, v in inputs.items() if v is not None and v != ""}

    def run(self, actor: str, did: str, aid: str, inputs: Any = None, row: Any = None) -> dict:
        self.boards.dashboard(actor, did)                         # only those who may open the dashboard
        a = self._get(did, aid)
        with self.engine.connect() as conn:
            config = json.loads(conn.execute(select(bi_actions.c.config).where(bi_actions.c.id == aid)).scalar_one())
        started = time.monotonic()
        status, detail, out = "ok", "", {}
        try:
            out = self._run(actor, a["kind"], config, inputs, row)
            detail = out.pop("detail", "")
        except (ActionError, InputTableError, WorkspaceError) as e:
            status, detail = ("refused" if e.status in (403, 404, 409) else "error"), str(e)
            self._record(aid, actor, status, detail, started)
            self._audit(actor, "bi.action_run", did, {"action": a["name"], "kind": a["kind"], "status": status})
            raise ActionError(str(e), e.status)
        except Exception as e:  # noqa: BLE001 - e.g. Trino refusing the write: recorded, then reported
            status_code = getattr(e, "status", 502)
            status, detail = ("refused" if status_code == 403 else "error"), str(e)[:500]
            self._record(aid, actor, status, detail, started)
            self._audit(actor, "bi.action_run", did, {"action": a["name"], "kind": a["kind"], "status": status})
            raise ActionError(str(e)[:500], status_code if isinstance(status_code, int) else 502)
        self._record(aid, actor, status, detail, started)
        self._audit(actor, "bi.action_run", did, {"action": a["name"], "kind": a["kind"], "status": status})
        return {"action": a["name"], "status": status, **out}

    def _run(self, actor: str, kind: str, config: dict, inputs: Any, row: Any) -> dict:
        if kind == "input_row":
            values = {**config["constants"], **self._inputs(config["fields"], inputs)}
            added = self.it.add(actor, config["input_table"], [values])
            return {"rows": added["added"], "detail": f"added a row to {config['table']}"}
        if kind == "input_update":
            if not isinstance(row, dict) or not isinstance(row.get("_row_id"), str):
                raise ActionError("row: the clicked row, with its _row_id and _updated_at")
            values = {**config["set"], **self._inputs(config["fields"], inputs)}
            self.it.change(actor, config["input_table"], row["_row_id"], values, row.get("_updated_at"))
            return {"row": row["_row_id"], "detail": f"changed {', '.join(sorted(values))} in {config['table']}"}
        if kind == "workflow":
            if config["who"] == "owner" and actor != config["workflow_owner"] and not self.is_operator(actor):
                raise ActionError("only the workflow's owner runs this action", 403)
            result = self.wf.run(config["workflow"], "action")
            steps = [{"name": s.get("name"), "status": s.get("status")} for s in result.get("steps") or []]
            ok = all(s["status"] == "ok" for s in steps)
            return {"steps": steps, "detail": "workflow ran" if ok else "workflow had failing steps"}
        if kind == "webhook":
            values = self._inputs(config["fields"], inputs)
            if not any(config["url"] == o or config["url"].startswith(o + "/") for o in self._origins()):
                raise ActionError("the webhook's origin is no longer allowed (CAIRN_ACTION_WEBHOOK_ORIGINS)", 403)
            body = json.dumps({"actor": actor, "inputs": values, "sent_ms": _now()}, sort_keys=True).encode()
            headers = {"Content-Type": "application/json"}
            secret = os.environ.get("CAIRN_ACTION_WEBHOOK_SECRET", "")
            if secret:
                headers["X-Cairn-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            code = self.post(config["url"], body, headers)
            if not 200 <= code < 300:
                raise ActionError(f"the API answered {code}", 502)
            return {"answered": code, "detail": f"API answered {code}"}
        raise ActionError(f"unknown action kind {kind}")

    @staticmethod
    def _post(url: str, body: bytes, headers: dict) -> int:
        import httpx

        return httpx.post(url, content=body, headers=headers, timeout=10, follow_redirects=False).status_code

    def _record(self, aid: str, actor: str, status: str, detail: str, started: float) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(bi_action_runs).values(id=uuid.uuid4().hex, action=aid, actor=actor, status=status,
                                                       detail=detail[:500], created_ms=_now(),
                                                       ms=int((time.monotonic() - started) * 1000)))

    def _audit(self, actor: str, action: str, did: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, f"dashboard:{did}", details)


def build_actions_router(a: Actions, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/dashboards/{did}/actions", tags=["sql"])

    def person(p) -> str:
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "dashboard actions are run by people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except (ActionError, WorkspaceError, InputTableError) as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(did: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: a.list(actor, did))

    @router.post("", status_code=201)
    async def add(did: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: a.add(actor, did, body))

    @router.post("/{aid}/run")
    async def run(did: str, aid: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: a.run(actor, did, aid, body.get("inputs"), body.get("row")))

    @router.get("/{aid}/runs")
    async def runs(did: str, aid: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: a.runs(actor, did, aid))

    @router.delete("/{aid}", status_code=204)
    async def remove(did: str, aid: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: a.remove(actor, did, aid))

    return router
