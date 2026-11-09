"""Change requests: infrastructure as code that agents (and people) propose and people approve (``/v1/iac/changes``,
the MCP tools ``iac_plan``, ``iac_propose``, ``iac_apply``, ``iac_open_pr``).

A change is a bundle (YAML, the sections of :mod:`cairn.iac.bundle`) or Terraform files (the subset
:mod:`cairn.iac.hcl` reads), plus ``delete: [{kind, key}]`` for what it removes. Its life:

1. **Planned** by the bundle engine through Cairn's own API, as the person (or agent) asking: what it would create,
   update, replace and delete, field by field, with no side effects.
2. **Proposed**: stored with that plan, the equivalent Terraform (for the record), who wrote it (the agent and the
   person it acts for, its *sponsor*) and why (its intent); approvers are told. Agents need the scope
   ``iac:propose``. A change never holds a secret's value: a secret (a write-only field) or a sensitive field is a
   reference, ``${NAME}``, given when the change is applied; anything that looks like a credential anywhere in it is
   refused. Deleting (or replacing) anything needs ``allow_delete: true`` in the change.
3. **Approved or rejected** by a person with ``APPROVE_IAC_CHANGE`` on every resource type it touches
   (``urn:li:iacKind:<kind>``), who must themselves be allowed to make the change: Cairn plans it again as them, and it
   must come out the same. The sponsor (or author) cannot approve their own change unless the operator allows it
   (``CAIRN_IAC_SELF_APPROVE=true``). Agents never approve.
4. **Applied** by the approver, as the approver: Cairn plans again and refuses when the plan changed since approval
   (drift); the change goes back to *proposed* with the new plan, to be approved again. Otherwise each resource is
   created, changed or deleted through Cairn's API as the approver (their permissions and audit apply), and the
   change is *applied*, or *failed* with what was done before the failure.

Every step is in the audit log (``iac.change_*``). When something cannot be checked (a privilege, a plan), the
answer is no.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.common.db import JSONText, metadata
from cairn.iac import bundle as bundles
from cairn.iac import registry

logger = logging.getLogger(__name__)

changes = Table(
    "cairn_iac_changes", metadata,
    Column("id", String(32), primary_key=True),
    Column("title", String(300), nullable=False),
    Column("status", String(16), nullable=False, index=True),   # proposed approved rejected applied failed withdrawn
    Column("author", String(256), nullable=False, index=True),  # the person who proposed it (or an agent acts for)
    Column("agent", String(64), nullable=True),                 # the agent that proposed it; None: a person did
    Column("sponsor", String(256), nullable=False, index=True),  # the person responsible for it
    Column("intent", Text, nullable=False),
    Column("bundle", Text, nullable=False),                     # the change as bundle YAML (references, never secrets)
    Column("terraform", Text, nullable=False),                  # the same as Terraform, for the record
    Column("allow_delete", Boolean, nullable=False),
    Column("plan", JSONText, nullable=False),
    Column("plan_digest", String(64), nullable=False),
    Column("kinds", JSONText, nullable=False),
    Column("approved_by", String(256), nullable=True),
    Column("approved_ms", BigInteger, nullable=True),
    Column("approved_digest", String(64), nullable=True),
    Column("comment", Text, nullable=True),                     # the decision's comment
    Column("applied_by", String(256), nullable=True),
    Column("applied_ms", BigInteger, nullable=True),
    Column("result", JSONText, nullable=True),
    Column("error", Text, nullable=True),
    Column("history", JSONText, nullable=False),                # [{at_ms, by, action, comment}]
    Column("commit", JSONText, nullable=True),                  # the git folder commit (iac_open_pr)
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

PRIVILEGE = "APPROVE_IAC_CHANGE"
STATUSES = ("proposed", "approved", "rejected", "applied", "failed", "withdrawn")
REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
MAX_TEXT = 512 * 1024
SENSITIVE = "(sensitive)"


def kind_urn(kind: str) -> str:
    return f"urn:li:iacKind:{kind}"


class ChangeError(Exception):
    def __init__(self, message: str, status: int = 400, plan: Optional[dict] = None):
        super().__init__(message)
        self.status, self.plan = status, plan


class CallError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str):
        super().__init__(f"{method} {path}: {status} {detail}")
        self.status, self.detail = status, detail


# ---- calling Cairn's API in-process, as someone ---------------------------------------------------------------
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="cairn-iac-call")
FORWARD = ("authorization", "cookie", "x-cairn-actor")


class AppCaller:
    """Calls this Cairn's API in-process (ASGI) with someone's credentials (their headers), from any thread: every
    permission check, policy and audit rule applies, as for a request of theirs.

    With ``principal`` instead, the calls are made as that principal, set in the ASGI scope (which no request from
    outside can set) and ``read_only``: how an agent's change is planned, reading Cairn as the person it acts for."""

    def __init__(self, app: Any, headers: Optional[dict[str, str]] = None, principal: Any = None,
                 read_only: bool = False):
        self.headers = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in FORWARD and v}
        self.read_only = read_only
        if principal is not None:
            inner = app

            async def as_principal(scope, receive, send):
                if scope.get("type") == "http":
                    scope = {**scope, "cairn.principal": principal}
                await inner(scope, receive, send)
            app, self.headers = as_principal, {}
        elif not self.headers:
            raise ChangeError("no credentials to act with", 401)
        self.app = app

    def _request(self, method: str, path: str, body: Any = None):
        import asyncio

        if self.read_only and method not in ("GET", "HEAD"):
            raise CallError(method, path, 403, "this caller only reads")

        import httpx

        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cairn.internal", timeout=120) as c:
                return await c.request(method, path, json=body, headers=self.headers)

        r = _POOL.submit(asyncio.run, go()).result(timeout=300)
        if r.status_code >= 300:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise CallError(method, path, r.status_code, str(detail)[:500])
        return r

    def call(self, method: str, path: str, body: Any = None) -> Any:
        r = self._request(method, path, body)
        return r.json() if r.content else None

    def text(self, method: str, path: str) -> str:
        return self._request(method, path).text


class _ChangeClient:
    """The bundle engine's client for a change: Cairn's API as the caller; the bundle state a change does not have
    (it owns nothing: deletes are named in it) is empty and never written."""

    def __init__(self, caller: Any, name: str):
        self.caller, self.name = caller, name

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if path.startswith("/v1/bundles/"):
            return {"name": self.name, "resources": {}, "versions": {}, "version": 0} if method == "GET" else {"version": 0}
        try:
            return self.caller.call(method, path, body)
        except CallError as e:
            raise bundles.ApiError(str(e), e.status)


# ---- reading a change ------------------------------------------------------------------------------------------
def read_change(bundle_yaml: Optional[str] = None, terraform_files: Optional[dict[str, str]] = None) -> dict:
    """The change as a bundle document, its deletes and ``allow_delete``: {"doc", "deletes", "allow_delete"}."""
    import yaml

    if bool(bundle_yaml) == bool(terraform_files):
        raise ChangeError("give the change as bundle_yaml or as terraform_files (one of them)")
    if bundle_yaml:
        if len(bundle_yaml) > MAX_TEXT:
            raise ChangeError("a change is at most 512 KB")
        try:
            raw = yaml.safe_load(bundle_yaml)
        except yaml.YAMLError as e:
            raise ChangeError(f"not YAML: {e}")
        if not isinstance(raw, dict):
            raise ChangeError("a change is a mapping: bundle sections (queries, flows, ...), delete and allow_delete")
    else:
        from cairn.iac.hcl import HclError, to_bundle

        if not isinstance(terraform_files, dict) or not all(isinstance(v, str) for v in terraform_files.values()):
            raise ChangeError("terraform_files is {file name: its text}")
        if sum(len(v) for v in terraform_files.values()) > MAX_TEXT:
            raise ChangeError("a change is at most 512 KB")
        try:
            raw = to_bundle(terraform_files)
        except HclError as e:
            raise ChangeError(f"Terraform: {e}")
    raw = dict(raw)
    deletes = raw.pop("delete", None) or []
    allow = raw.pop("allow_delete", False)
    if not isinstance(allow, bool):
        raise ChangeError("allow_delete is true or false")
    if not isinstance(deletes, list) or not all(isinstance(d, dict) and set(d) == {"kind", "key"} for d in deletes):
        raise ChangeError("delete is a list of {kind, key}: the resources the change removes")
    kinds = registry.by_name()
    for d in deletes:
        if d["kind"] not in kinds:
            raise ChangeError(f"delete: no resource type {d['kind']}")
        if not isinstance(d["key"], str) or not d["key"]:
            raise ChangeError(f"delete: {d['kind']}: key is the resource's name (as a bundle names it)")
    raw.setdefault("bundle", "change")
    try:
        doc = bundles.parse(raw, substitute=False)
    except bundles.BundleError as e:
        raise ChangeError(str(e))
    _no_secrets(doc)
    return {"doc": doc, "raw": raw, "deletes": deletes, "allow_delete": allow}


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def _no_secrets(doc: dict) -> None:
    """A change never holds a secret: secrets and sensitive fields are ``${NAME}`` references; nothing else may look
    like a credential."""
    from cairn.ai.guardrails import secret_kinds

    for kind, k in bundles.KINDS.items():
        for key, spec in doc.get(kind, {}).items():
            for f in k.fields:
                if f.name not in spec or spec[f.name] in (None, "", []):
                    continue
                if f.write_only or f.sensitive:
                    vals = spec[f.name] if isinstance(spec[f.name], list) else [spec[f.name]]
                    if not all(isinstance(x, str) and REF.match(x) for x in vals):
                        raise ChangeError(f"{kind}/{key}: {f.name} is a secret: write a reference such as ${{NAME}} "
                                          "(its value is given when the change is applied), never the value", 422)
            for text in _strings(spec):
                found = secret_kinds(text)
                if found:
                    raise ChangeError(f"{kind}/{key}: it looks like it holds a credential ({', '.join(found)}); a change "
                                      "names secrets (${NAME}, or an existing Cairn secret), never their values", 422)


def _substitute(doc: dict, values: dict[str, str]) -> dict:
    """References replaced with the values given when the change is applied (never the server's environment)."""
    missing: set[str] = set()

    def one(v: Any) -> Any:
        if isinstance(v, str):
            def sub(m):
                if m.group(1) not in values:
                    missing.add(m.group(1))
                    return m.group(0)
                return str(values[m.group(1)])
            return bundles.VAR.sub(sub, v.replace("$${", bundles.ESCAPED)).replace(bundles.ESCAPED, "${")
        if isinstance(v, list):
            return [one(x) for x in v]
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        return v
    out = {kind: ({key: one(spec) for key, spec in specs.items()} if kind in bundles.KINDS else specs)
           for kind, specs in doc.items()}
    if missing:
        raise ChangeError(f"give the values of {', '.join(sorted(missing))} (variables) to apply this change", 422)
    return out


def references(doc: dict) -> list[str]:
    return sorted({m.group(1) for kind in bundles.KINDS for spec in doc.get(kind, {}).values()
                   for text in _strings(spec) for m in bundles.VAR.finditer(text.replace("$${", ""))})


# ---- plans ------------------------------------------------------------------------------------------------------
def _masked(k: registry.Kind, field: str, v: Any) -> Any:
    try:
        f = k.field(field)
    except KeyError:
        return v
    if (f.write_only or f.sensitive) and v not in (None, "", []):
        return SENSITIVE
    return v


def structured(items: list[bundles.Change]) -> dict:
    """A plan as the UI and agents read it: each resource, what happens to it, and each field before and after."""
    out = []
    for c in items:
        k = bundles.KINDS[c.kind]
        if c.action == "keep":
            continue
        if c.action == "create":
            fields = [{"field": f, "before": None, "after": _masked(k, f, v)} for f, v in (c.desired or {}).items()
                      if f != "owner"]
        elif c.action == "delete":
            from cairn.iac.engine import declared

            fields = [{"field": f, "before": _masked(k, f, v), "after": None}
                      for f, v in declared(k, c.current or {}).items()]
        else:
            fields = [{"field": f, "before": _masked(k, f, a), "after": _masked(k, f, b)} for f, (a, b) in c.diff.items()]
        out.append({"action": c.action, "kind": c.kind, "key": c.key, "fields": fields,
                    "terraform_type": f"cairn_{k.tf}"})
    counts = {a: sum(1 for x in out if x["action"] == a) for a in ("create", "update", "replace", "delete")}
    unchanged = sum(1 for c in items if c.action == "keep")
    digest = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
    return {"changes": out, "counts": {**counts, "unchanged": unchanged}, "digest": digest}


def plan_change(caller: Any, change: dict) -> tuple[dict, list[bundles.Change]]:
    """(the structured plan, the bundle engine's changes) for a read change, as ``caller``. No side effects."""
    from cairn.iac.engine import Engine

    doc = change["doc"]
    client = _ChangeClient(caller, doc["bundle"])
    try:
        items, _ = bundles.plan(client, doc, prune=False)
    except bundles.ApiError as e:
        raise ChangeError(f"Cairn refused to plan it as you: {e}", 403 if e.status in (401, 403) else 409)
    except bundles.BundleError as e:
        raise ChangeError(str(e), 409)
    if change["deletes"]:
        engine = Engine(client.call)
        me = None
        for d in change["deletes"]:
            k = bundles.KINDS[d["kind"]]
            if any(c.kind == d["kind"] and c.key == d["key"] for c in items if c.action != "keep"):
                raise ChangeError(f"{d['kind']}/{d['key']} is both changed and deleted")
            try:
                rows = bundles._rows(engine, k, {k.name: {}}, {k.name: {d["key"]}})
            except bundles.ApiError as e:
                raise ChangeError(f"Cairn refused to read {d['kind']} as you: {e}", 403 if e.status in (401, 403) else 409)
            if k.mine:
                me = me or client.call("GET", "/me")["urn"]
                rows = [r for r in rows if r.get(k.owner_field) == me]
            found = [r for r in rows if bundles._rec_key(k, r) == d["key"]]
            if not found:
                raise ChangeError(f"delete: no {d['kind']} {d['key']!r} that you may see", 404)
            items.append(bundles.Change("delete", d["kind"], d["key"], current=found[0]))
    return structured(items), items


def _deletes(plan: dict) -> list[str]:
    return [f"{c['kind']}/{c['key']}" for c in plan["changes"] if c["action"] in ("delete", "replace")]


# ---- the service ------------------------------------------------------------------------------------------------
def _now() -> int:
    return int(time.time() * 1000)


class ChangeRequests:
    def __init__(self, engine, audit=None, authorize: Optional[Callable[..., bool]] = None,
                 is_admin: Optional[Callable[[str], bool]] = None, notify: Optional[Callable[..., Any]] = None,
                 git_folders: Any = None, app: Optional[Callable[[], Any]] = None):
        """``authorize(principal, privilege, urn) -> bool`` (False when it cannot tell); ``notify(type, title, body,
        recipients, payload)``; ``app()``: this Cairn's API (ASGI) for acting as someone."""
        self.engine, self.audit, self.notify, self.git_folders = engine, audit, notify, git_folders
        self.authorize = authorize or (lambda p, privilege, urn: False)
        self.is_admin = is_admin or (lambda actor: False)
        self._app = app
        self.self_approve = os.environ.get("CAIRN_IAC_SELF_APPROVE", "").lower() in ("1", "true", "yes")
        self.approvers = [x.strip() for x in os.environ.get("CAIRN_IAC_CHANGE_APPROVERS", "").split(",") if x.strip()]

    def caller(self, headers: dict[str, str]) -> AppCaller:
        if self._app is None:
            raise ChangeError("Cairn's API is not available to act with here", 503)
        return AppCaller(self._app(), headers)

    def plan_caller(self, p: Any, headers: dict[str, str]) -> AppCaller:
        """Who a plan reads Cairn as: the person, with their own credentials; for an agent, the person it acts for,
        read-only (some of Cairn's APIs are for people only, and a plan only reads)."""
        if getattr(p, "via", "") != "agent":
            return self.caller(headers)
        if self._app is None:
            raise ChangeError("Cairn's API is not available to act with here", 503)
        from cairn.common.security import Principal

        return AppCaller(self._app(), principal=Principal(p.urn, "delegated"), read_only=True)

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _audit(self, actor: str, action: str, cid: Optional[str], details: dict) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("iac", action, actor, f"iac-change:{cid}" if cid else None, details)
        except Exception:  # noqa: BLE001 - logged; never stops the change itself
            logger.exception("iac: audit %s failed", action)

    def _tell(self, kind: str, title: str, body: str, who: list[str], cid: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(kind, title, body, sorted(set(who)), {"change": cid})
        except Exception:  # noqa: BLE001 - the change stands either way
            logger.exception("iac: could not tell about change %s", cid)

    @staticmethod
    def _person(p: Any) -> str:
        if getattr(p, "is_service", False) or getattr(p, "via", "") in ("extension", "service"):
            raise ChangeError("change requests are made and decided by people and their agents", 403)
        return p.urn

    def _row(self, cid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(changes).where(changes.c.id == cid)).first()
        if r is None:
            raise ChangeError("no such change request", 404)
        return dict(r._mapping)

    def _may_approve_kinds(self, p: Any, kinds: list[str]) -> list[str]:
        """The kinds of a change ``p`` may not approve (all of them when the policy cannot be asked)."""
        refused = []
        for kind in kinds:
            try:
                if not self.authorize(p, PRIVILEGE, kind_urn(kind)):
                    refused.append(kind)
            except Exception:  # noqa: BLE001 - cannot tell: no (fail closed)
                refused.append(kind)
        return refused

    def _visible(self, p: Any, row: dict) -> bool:
        if p.urn in (row["author"], row["sponsor"], row["approved_by"], row["applied_by"]):
            return True
        if getattr(p, "via", "") == "agent":
            return False
        try:
            if self.is_admin(p.urn):
                return True
        except Exception:  # noqa: BLE001 - cannot tell: not an admin
            pass
        kinds = list(row["kinds"] or [])
        return bool(kinds) and len(self._may_approve_kinds(p, kinds)) < len(kinds)   # may approve some of it

    def public(self, row: dict, full: bool = True) -> dict:
        out = {k: row[k] for k in ("id", "title", "status", "author", "agent", "sponsor", "intent", "allow_delete",
                                   "kinds", "approved_by", "approved_ms", "comment", "applied_by", "applied_ms",
                                   "error", "created_ms", "updated_ms", "commit")}
        out["plan"] = row["plan"] if full else {"counts": (row["plan"] or {}).get("counts")}
        if full:
            out.update(bundle=row["bundle"], terraform=row["terraform"], history=row["history"], result=row["result"],
                       variables=references(read_change(row["bundle"])["doc"]))
        return out

    def _history(self, row: dict, by: str, action: str, comment: str = "", **extra: Any) -> list[dict]:
        return [*(row["history"] or []), {"at_ms": _now(), "by": by, "action": action,
                                          **({"comment": comment} if comment else {}), **extra}]

    # ---- plan and propose ---------------------------------------------------------------------------------------
    def plan(self, p: Any, headers: dict, bundle_yaml: Optional[str] = None,
             terraform_files: Optional[dict[str, str]] = None) -> dict:
        """What the change would do, as ``p``, with no side effects."""
        self._person(p)
        change = read_change(bundle_yaml, terraform_files)
        plan, _ = plan_change(self.plan_caller(p, headers), change)
        return {**plan, "allow_delete": change["allow_delete"], "needs_allow_delete": _deletes(plan),
                "variables": references(change["doc"])}

    def propose(self, p: Any, headers: dict, title: str, intent: str, bundle_yaml: Optional[str] = None,
                terraform_files: Optional[dict[str, str]] = None, allow_delete: Optional[bool] = None,
                agent: Optional[str] = None) -> dict:
        """Stores a change request with its plan (made as ``p``). ``agent``: the built-in assistant that drafted it for
        ``p`` (an agent credential names its own)."""
        actor = self._person(p)
        agent = agent or getattr(p, "agent", None)
        if getattr(p, "via", "") == "agent" and "iac:propose" not in (getattr(p, "scopes", None) or ()):
            self._audit(actor, "iac.change_refused", None, {"agent": agent, "why": "the agent has no iac:propose scope"})
            raise ChangeError("this agent's scopes do not include iac:propose", 403)
        title = " ".join(str(title or "").split())[:300]
        intent = str(intent or "").strip()[:4000]
        if not title or not intent:
            raise ChangeError("a change request has a title and an intent (why it is wanted)")
        try:
            change = read_change(bundle_yaml, terraform_files)
        except ChangeError as e:
            self._audit(actor, "iac.change_refused", None, {"agent": agent, "title": title, "why": str(e)})
            raise
        if allow_delete is not None:
            change["allow_delete"] = change["allow_delete"] or bool(allow_delete)
        plan, _ = plan_change(self.plan_caller(p, headers), change)
        if not plan["changes"]:
            raise ChangeError("the change would change nothing: everything in it is already so", 409)
        if _deletes(plan) and not change["allow_delete"]:
            why = f"it deletes or replaces {', '.join(_deletes(plan))}: say allow_delete: true in the change"
            self._audit(actor, "iac.change_refused", None, {"agent": agent, "title": title, "why": why})
            raise ChangeError(why, 422, plan)
        doc = change["doc"]
        text = _bundle_yaml(change["raw"], change["deletes"], change["allow_delete"])
        terraform = _terraform(doc)
        cid, now = uuid.uuid4().hex, _now()
        kinds = sorted({c["kind"] for c in plan["changes"]})
        row = {"id": cid, "title": title, "status": "proposed", "author": actor, "agent": agent, "sponsor": actor,
               "intent": intent, "bundle": text, "terraform": terraform, "allow_delete": change["allow_delete"],
               "plan": plan, "plan_digest": plan["digest"], "kinds": kinds, "approved_by": None, "approved_ms": None,
               "approved_digest": None, "comment": None, "applied_by": None, "applied_ms": None, "result": None,
               "error": None, "history": [{"at_ms": now, "by": actor, "action": "proposed",
                                           **({"agent": agent} if agent else {})}],
               "commit": None, "created_ms": now, "updated_ms": now}
        with self.engine.begin() as conn:
            conn.execute(changes.insert().values(**row))
        self._audit(actor, "iac.change_proposed", cid, {"title": title, "agent": agent, "kinds": kinds,
                                                        "counts": plan["counts"], "digest": plan["digest"]})
        self._tell("iac.change_proposed", f"Change proposed: {title}",
                   f"{'The agent ' + agent + ' for ' if agent else ''}{actor.split(':')[-1]} proposes: {intent[:500]}\n"
                   f"{_summary(plan)}", [*self.approvers, actor], cid)
        return self.public(row)

    # ---- reading ----------------------------------------------------------------------------------------------
    def list(self, p: Any, status: Optional[str] = None) -> list[dict]:
        self._person(p)
        q = select(changes).order_by(changes.c.created_ms.desc()).limit(500)
        if status:
            if status not in STATUSES:
                raise ChangeError(f"status is one of {', '.join(STATUSES)}")
            q = q.where(changes.c.status == status)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(q)]
        return [self.public(r, full=False) for r in rows if self._visible(p, r)]

    def get(self, p: Any, cid: str) -> dict:
        self._person(p)
        row = self._row(cid)
        if not self._visible(p, row):
            raise ChangeError("no such change request", 404)
        out = self.public(row)
        out["may_approve"] = row["status"] == "proposed" and self._may_decide(p, row) is None
        out["may_apply"] = row["status"] == "approved" and p.urn == row["approved_by"]
        return out

    # ---- deciding ---------------------------------------------------------------------------------------------
    def _may_decide(self, p: Any, row: dict) -> Optional[str]:
        """Why ``p`` may not approve ``row`` (None: they may, as far as the policy says; the plan is checked too)."""
        if getattr(p, "via", "") == "agent":
            return "agents never approve changes; a person does"
        if p.urn in (row["sponsor"], row["author"]) and not self.self_approve:
            return ("you proposed this change (or your agent did): someone else approves it (separation of duties; "
                    "CAIRN_IAC_SELF_APPROVE lets operators allow it)")
        refused = self._may_approve_kinds(p, list(row["kinds"] or []))
        if refused:
            return f"you may not approve changes to {', '.join(refused)} ({PRIVILEGE})"
        return None

    def approve(self, p: Any, headers: dict, cid: str, comment: str = "") -> dict:
        actor = self._person(p)
        row = self._row(cid)
        if not self._visible(p, row):
            self._audit(actor, "iac.change_approval_refused", cid, {"why": "not theirs to see or approve"})
            raise ChangeError("no such change request", 404)
        if row["status"] != "proposed":
            raise ChangeError(f"the change is {row['status']}, not proposed", 409)
        why = self._may_decide(p, row)
        if why:
            self._audit(actor, "iac.change_approval_refused", cid, {"why": why})
            raise ChangeError(why, 403)
        change = read_change(row["bundle"])
        try:
            plan, _ = plan_change(self.caller(headers), change)    # as the approver: they could make it themselves
        except ChangeError as e:
            self._audit(actor, "iac.change_approval_refused", cid, {"why": str(e)})
            raise ChangeError(f"you may not make this change yourself: {e}", 403)
        if plan["digest"] != row["plan_digest"]:
            self._audit(actor, "iac.change_approval_refused", cid, {"why": "the plan as the approver differs",
                                                                    "digest": plan["digest"]})
            raise ChangeError("as you, this change would do something else (see plan): approve only what you could "
                              "make yourself, or plan it again", 409, plan)
        comment = str(comment or "")[:2000]
        now = _now()
        with self.engine.begin() as conn:
            n = conn.execute(update(changes).where(changes.c.id == cid, changes.c.status == "proposed").values(
                status="approved", approved_by=actor, approved_ms=now, approved_digest=plan["digest"], comment=comment,
                updated_ms=now, history=self._history(row, actor, "approved", comment))).rowcount
        if not n:
            raise ChangeError("the change was decided meanwhile", 409)
        self._audit(actor, "iac.change_approved", cid, {"digest": plan["digest"], "comment": comment})
        self._tell("iac.change_approved", f"Change approved: {row['title']}",
                   f"{actor.split(':')[-1]} approved it" + (f": {comment}" if comment else "") + ". They apply it.",
                   [row["sponsor"], actor], cid)
        return self.get(p, cid)

    def reject(self, p: Any, cid: str, comment: str = "") -> dict:
        actor = self._person(p)
        row = self._row(cid)
        if not self._visible(p, row):
            raise ChangeError("no such change request", 404)
        if row["status"] not in ("proposed", "approved"):
            raise ChangeError(f"the change is {row['status']}", 409)
        withdraw = actor in (row["sponsor"], row["author"])
        if not withdraw:
            why = self._may_decide(p, row)
            if why:
                self._audit(actor, "iac.change_rejection_refused", cid, {"why": why})
                raise ChangeError(why, 403)
        status = "withdrawn" if withdraw else "rejected"
        comment = str(comment or "")[:2000]
        now = _now()
        with self.engine.begin() as conn:
            n = conn.execute(update(changes).where(changes.c.id == cid, changes.c.status == row["status"]).values(
                status=status, comment=comment, updated_ms=now,
                history=self._history(row, actor, status, comment))).rowcount
        if not n:
            raise ChangeError("the change was decided meanwhile", 409)
        self._audit(actor, f"iac.change_{status}", cid, {"comment": comment})
        if not withdraw:
            self._tell("iac.change_rejected", f"Change rejected: {row['title']}",
                       f"{actor.split(':')[-1]} rejected it" + (f": {comment}" if comment else ""), [row["sponsor"]], cid)
        return self.get(p, cid)

    def delete(self, p: Any, cid: str) -> None:
        """Withdraws a change that was not applied (its author or sponsor); applied ones stay, for the record."""
        actor = self._person(p)
        row = self._row(cid)
        if actor not in (row["sponsor"], row["author"]):
            raise ChangeError("no such change request of yours", 404)
        if row["status"] in ("applied", "failed"):
            raise ChangeError("an applied change stays, for the record", 409)
        if row["status"] != "withdrawn":
            self.reject(p, cid, "withdrawn")

    # ---- applying ---------------------------------------------------------------------------------------------
    def apply(self, p: Any, headers: dict, cid: str, values: Optional[dict] = None) -> dict:
        """Applies an approved change as its approver. ``values``: the referenced variables (secrets), used for this
        apply only: never stored, logged or audited."""
        actor = self._person(p)
        row = self._row(cid)
        if not self._visible(p, row):
            raise ChangeError("no such change request", 404)
        if getattr(p, "via", "") == "agent":
            raise ChangeError("agents never apply changes; the approver does", 403)
        if row["status"] != "approved":
            raise ChangeError(f"the change is {row['status']}: only an approved change is applied", 409)
        if actor != row["approved_by"]:
            raise ChangeError(f"{row['approved_by'].split(':')[-1]} approved it, and applies it (as themselves)", 403)
        values = {str(k): str(v) for k, v in (values or {}).items()}
        change = read_change(row["bundle"])
        doc = _substitute(change["doc"], values)
        caller = self.caller(headers)
        try:
            plan, items = plan_change(caller, {**change, "doc": doc})
        except ChangeError as e:
            self._audit(actor, "iac.change_apply_refused", cid, {"why": str(e)})
            raise
        if plan["digest"] != row["approved_digest"]:
            now = _now()
            with self.engine.begin() as conn:
                conn.execute(update(changes).where(changes.c.id == cid, changes.c.status == "approved").values(
                    status="proposed", plan=plan, plan_digest=plan["digest"], approved_by=None, approved_ms=None,
                    approved_digest=None, updated_ms=now,
                    history=self._history(row, actor, "drift", "the plan changed since it was approved"),
                    kinds=sorted({c["kind"] for c in plan["changes"]})))
            self._audit(actor, "iac.change_drift", cid, {"approved_digest": row["approved_digest"],
                                                         "digest": plan["digest"]})
            self._tell("iac.change_drift", f"Change needs approval again: {row['title']}",
                       "What it would do changed since it was approved:\n" + _summary(plan),
                       [row["sponsor"], actor, *self.approvers], cid)
            raise ChangeError("the plan changed since the change was approved (drift): it is proposed again with the new "
                              "plan, for approval", 409, plan)
        lines: list[str] = []
        self._audit(actor, "iac.change_apply_started", cid, {"digest": plan["digest"]})
        try:
            bundles.apply(_ChangeClient(caller, doc["bundle"]), doc, items, {"resources": {}, "versions": {}, "version": 0},
                          log=lines.append)
            status, error = "applied", None
        except (bundles.BundleError, CallError) as e:
            status, error = "failed", str(e)[:4000]
        now = _now()
        result = {"done": lines, "counts": plan["counts"]}
        with self.engine.begin() as conn:
            conn.execute(update(changes).where(changes.c.id == cid).values(
                status=status, applied_by=actor, applied_ms=now, result=result, error=error, updated_ms=now,
                history=self._history(row, actor, status, error or "")))
        self._audit(actor, f"iac.change_{status}", cid, {"done": lines, **({"error": error} if error else {})})
        self._tell(f"iac.change_{status}", f"Change {status}: {row['title']}",
                   "\n".join(lines) + (f"\n{error}" if error else ""), [row["sponsor"], actor], cid)
        if status == "failed":
            raise ChangeError(f"applying stopped: {error} (done before it: {len(lines)})", 502)
        return self.get(p, cid)

    # ---- git ------------------------------------------------------------------------------------------------------
    def open_pr(self, p: Any, cid: str, git_folder: str) -> dict:
        """Commits the change (its bundle, its Terraform and its plan) to a new branch of one of the person's git
        folders, with a pull request where the repository is configured for it: a reviewable commit."""
        actor = self._person(p)
        row = self._row(cid)
        if not self._visible(p, row):
            raise ChangeError("no such change request", 404)
        if getattr(p, "via", "") == "agent" and "iac:propose" not in (getattr(p, "scopes", None) or ()):
            raise ChangeError("this agent's scopes do not include iac:propose", 403)
        if self.git_folders is None:
            raise ChangeError("git folders are not set up here", 503)
        from cairn.sqlworkspace.workspace import WorkspaceError

        base = f"iac/changes/{cid[:12]}"
        files = {f"{base}/change.yaml": row["bundle"], f"{base}/main.tf": row["terraform"],
                 f"{base}/PLAN.md": _plan_md(row)}
        try:
            out = self.git_folders.commit_files(actor, git_folder, files, f"{row['title']}\n\n{row['intent']}\n\n"
                                                f"Cairn change request {cid}", new_branch=f"iac/change-{cid[:12]}",
                                                pr_body=_plan_md(row))
        except WorkspaceError as e:
            raise ChangeError(str(e), getattr(e, "status", 400))
        with self.engine.begin() as conn:
            conn.execute(update(changes).where(changes.c.id == cid).values(
                commit=out, updated_ms=_now(), history=self._history(row, actor, "committed", "",
                                                                     branch=out.get("branch"))))
        self._audit(actor, "iac.change_committed", cid, {"folder": git_folder, "branch": out.get("branch"),
                                                         "commit": out.get("commit")})
        return out


def _summary(plan: dict) -> str:
    lines = []
    for c in plan["changes"][:30]:
        sign = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}.get(c["action"], " ")
        lines.append(f"{sign} {c['kind']}/{c['key']}")
    if len(plan["changes"]) > 30:
        lines.append(f"... and {len(plan['changes']) - 30} more")
    return "\n".join(lines)


def _plan_md(row: dict) -> str:
    out = [f"# {row['title']}", "", row["intent"], "",
           f"Proposed by {row['sponsor'].split(':')[-1]}" + (f" through the agent {row['agent']}" if row["agent"] else "")
           + f" (Cairn change request `{row['id']}`, {row['status']}).", "", "## Plan", ""]
    for c in row["plan"]["changes"]:
        out.append(f"* **{c['action']}** `{c['kind']}/{c['key']}`")
        for f in c["fields"]:
            out.append(f"  * `{f['field']}`: `{json.dumps(f['before'])}` -> `{json.dumps(f['after'])}`")
    return "\n".join(out) + "\n"


def _bundle_yaml(raw: dict, deletes: list[dict], allow_delete: bool) -> str:
    """The change as it was written (each section in the registry's order), so it reads back the same."""
    import yaml

    out: dict[str, Any] = {"bundle": raw.get("bundle") or "change"}
    for kind in bundles.ORDER:
        if raw.get(kind):
            out[kind] = raw[kind]
    if deletes:
        out["delete"] = deletes
    if allow_delete:
        out["allow_delete"] = True
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120)


def _terraform(doc: dict) -> str:
    from cairn.iac.export import to_terraform

    items = [(bundles.KINDS[kind], spec) for kind in bundles.ORDER for spec in doc.get(kind, {}).values()]
    try:
        return to_terraform(items, declared_values=True)
    except Exception as e:  # noqa: BLE001 - the bundle is what is applied; Terraform is for the record
        return f"# The same change as Terraform could not be written: {e}\n"


# ---- HTTP ---------------------------------------------------------------------------------------------------------
def build_changes_router(svc: ChangeRequests, current_principal, assistant=None) -> Any:
    from fastapi import APIRouter, Body, Depends, Query
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/iac/changes", tags=["iac"])

    async def wrap(fn):
        try:
            return await run_in_threadpool(fn)
        except ChangeError as e:
            return JSONResponse({"detail": str(e), **({"plan": e.plan} if e.plan else {})}, status_code=e.status)

    def headers_of(request) -> dict:
        return {k: v for k, v in request.headers.items() if k.lower() in FORWARD}

    @router.get("")
    async def list_changes(status: Optional[str] = Query(None), p=Depends(current_principal)):
        return await wrap(lambda: svc.list(p, status))

    @router.post("/plan")
    async def plan(request: Request, body: dict = Body(...), p=Depends(current_principal)):
        """What a change would do, as you: creates, updates, replaces and deletes, field by field. No side effects."""
        h = headers_of(request)
        return await wrap(lambda: svc.plan(p, h, body.get("bundle_yaml"), body.get("terraform_files")))

    @router.post("", status_code=201)
    async def propose(request: Request, body: dict = Body(...), p=Depends(current_principal)):
        h = headers_of(request)
        return await wrap(lambda: svc.propose(p, h, body.get("title") or "", body.get("intent") or "",
                                              body.get("bundle_yaml"), body.get("terraform_files"),
                                              body.get("allow_delete")))

    @router.post("/draft")
    async def draft(request: Request, body: dict = Body(...), p=Depends(current_principal)):
        """Ask an agent: a change described in plain words, drafted by Cairn's assistant through the AI gateway with
        the IaC tools, as you, and proposed for approval."""
        if assistant is None:
            return JSONResponse({"detail": "Ask an agent is not set up here"}, status_code=503)
        h = headers_of(request)
        return await wrap(lambda: assistant.draft(p, h, str(body.get("prompt") or "")))

    @router.get("/assistant")
    async def assistant_status(p=Depends(current_principal)):
        """Whether Ask an agent can draft changes here (a model through the AI gateway), and if not, why."""
        if assistant is None:
            return {"ready": False, "model": None, "why": "Ask an agent is not set up here"}
        return assistant.status()

    @router.get("/{cid}")
    async def get(cid: str, p=Depends(current_principal)):
        return await wrap(lambda: svc.get(p, cid))

    @router.post("/{cid}/approve")
    async def approve(cid: str, request: Request, body: dict = Body(default={}), p=Depends(current_principal)):
        h = headers_of(request)
        return await wrap(lambda: svc.approve(p, h, cid, str(body.get("comment") or "")))

    @router.post("/{cid}/reject")
    async def reject(cid: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await wrap(lambda: svc.reject(p, cid, str(body.get("comment") or "")))

    @router.post("/{cid}/apply")
    async def apply(cid: str, request: Request, body: dict = Body(default={}), p=Depends(current_principal)):
        h = headers_of(request)
        values = body.get("variables") or {}
        if not isinstance(values, dict):
            return JSONResponse({"detail": "variables is {NAME: value}"}, status_code=422)
        return await wrap(lambda: svc.apply(p, h, cid, values))

    @router.post("/{cid}/commit")
    async def commit(cid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await wrap(lambda: svc.open_pr(p, cid, str(body.get("git_folder") or "")))

    @router.delete("/{cid}", status_code=204)
    async def withdraw(cid: str, p=Depends(current_principal)):
        out = await wrap(lambda: svc.delete(p, cid))
        return out if out is not None else None

    return router

