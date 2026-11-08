"""AI data packages and agents on shared data: a lakehouse share with what its recipient's agents need to understand
it, and the provider's own agents answering the recipient over the shared tables only.

**Data packages.** ``PUT /v1/shares/<share>/package`` with ``{"metrics": [names], "sample_questions": [...],
"notes": "..."}`` publishes a new version of the share's package: a manifest of the shared tables with their
descriptions, columns (type, description, glossary terms), the glossary terms' definitions, the governed metrics named
(each must read a shared table), sample questions and the agents attached. The manifest is signed by Cairn (a compact
JWS, RS256, ``typ`` ``cairn-data-package+jws``, with the key published at ``/v1/trino/jwks.json``). Only what is
shared is described: a metric over a table the share does not include is refused. Versions are kept; each publish is
audited. The recipient fetches it with the share's credential: ``GET /v1/shares/recipient/package`` (the share's
OAuth token as a bearer, or the credential itself as HTTP Basic), and checks the signature against the JWKS.

**Agents on shared data.** ``POST /v1/shares/<share>/agents`` with ``{"app": "<agent app>"}`` attaches one of the
provider's deployed agent apps (``/v1/agent-apps``; the provider must be allowed to use it). The recipient asks it with
``POST /v1/shares/recipient/agents/<app>/invocations`` (``{"input", "session_id"}``, the share's credential):

* the run's actor is the recipient (``urn:li:corpuser:share:<id>``), identity type ``share-recipient``;
* its Cairn run token is narrowed to the ``model`` scope (nothing else in Cairn), and its model calls are allowed or
  refused as for the provider who attached the agent, and metered to the recipient;
* its data comes only through a lakehouse token for the share's own identity (``ctx.catalog()`` in the agent):
  Cairn Catalog asks Cairn about every request, so the agent sees the shared tables, read-only, and nothing else;
* each invocation is recorded per recipient (``GET /v1/shares/<share>/agents`` sums runs, failures and time) and
  audited (``share.agent_invoked``).

Revoking the share (or its expiry) stops both at once: the package and the agents are refused, and the lakehouse
token the agent holds stops working on its next request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, PrimaryKeyConstraint, String, Table, Text, insert, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.dataaccess.shares import AUDIENCE, PREFIX, ShareError, _hash, _split

logger = logging.getLogger(__name__)

TYP = "cairn-data-package+jws"

share_packages = Table(
    "cairn_share_packages", metadata,
    Column("share_id", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("manifest", Text, nullable=False),               # JSON, as signed
    Column("signature", Text, nullable=False),              # the compact JWS of the manifest
    Column("digest", String(64), nullable=False),           # sha256 of the canonical manifest
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "version"),
)
share_agents = Table(
    "cairn_share_agents", metadata,
    Column("share_id", String(32), nullable=False),
    Column("app", String(48), nullable=False),
    Column("attached_by", String(256), nullable=False),
    Column("attached_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "app"),
)
share_agent_runs = Table(
    "cairn_share_agent_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("share_id", String(32), nullable=False, index=True),
    Column("app", String(48), nullable=False),
    Column("session_id", String(128), nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("ms", BigInteger, nullable=False),
    Column("input_chars", Integer, nullable=False),
    Column("output_chars", Integer, nullable=False),
    Column("started_ms", BigInteger, nullable=False, index=True),
)


def _now() -> int:
    return int(time.time() * 1000)


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()


def recipient_urn(share_id: str) -> str:
    return f"urn:li:corpuser:{PREFIX}{share_id}"


class SharePackages:
    def __init__(self, engine, shares, graph=None, metrics=None, agent_apps=None, audit=None):
        """``shares``: the lakehouse shares (their tokens sign the manifests); ``graph``: the catalog (descriptions,
        columns, glossary); ``metrics``: the metrics layer, or a function giving it (it may be built later);
        ``agent_apps``: the deployed agents that may be attached."""
        self.engine, self.shares, self.graph, self._metrics = engine, shares, graph, metrics
        self.agent_apps, self.audit = agent_apps, audit

    @property
    def metrics(self):
        return self._metrics() if callable(self._metrics) else self._metrics

    # ---- the provider's side ------------------------------------------------------------------------
    def _aspect(self, urn: str, name: str) -> dict:
        if self.graph is None:
            return {}
        try:
            return self.graph.get_aspect(urn, name) or {}
        except Exception as e:  # noqa: BLE001 - a package without a description is still a package
            logger.warning("package: %s of %s: %s", name, urn, e)
            return {}

    def _table(self, name: str, terms: dict[str, dict]) -> dict:
        urn = self.shares.lake.urn(*_split(name))
        props = self._aspect(urn, "datasetProperties")
        editable = {f.get("fieldPath"): f for f in (self._aspect(urn, "editableSchemaMetadata")
                                                    .get("editableSchemaFieldInfo") or [])}
        columns = []
        for f in self._aspect(urn, "schemaMetadata").get("fields") or []:
            path = str(f.get("fieldPath") or "")
            if not path:
                continue
            ed = editable.get(path) or {}
            term_urns = [t.get("urn") for t in ((f.get("glossaryTerms") or {}).get("terms") or [])
                         + ((ed.get("glossaryTerms") or {}).get("terms") or []) if t.get("urn")]
            for t in term_urns:
                terms.setdefault(t, {})
            columns.append({"name": path, "type": f.get("nativeDataType") or (f.get("type") or {}).get("type"),
                            "description": ed.get("description") or f.get("description") or "",
                            "glossary_terms": sorted(set(term_urns))})
        editable_props = self._aspect(urn, "editableDatasetProperties")
        for t in (self._aspect(urn, "glossaryTerms").get("terms") or []):
            if t.get("urn"):
                terms.setdefault(t["urn"], {})
        return {"name": name, "urn": urn,
                "description": editable_props.get("description") or props.get("description") or "",
                "columns": columns}

    def _metric(self, name: str, shared: set[str]) -> dict:
        if self.metrics is None:
            raise ShareError("the metrics layer is not configured", 409)
        from cairn.sqlworkspace.metrics import MetricError, sql_expression

        try:
            m = self.metrics.get(name)
        except MetricError:
            raise ShareError(f"no metric {name}", 404)
        table = str(m["table"]).lower()
        if table not in shared:
            raise ShareError(f"metric {name} reads {m['table']}, which this share does not include", 409)
        try:
            expression = sql_expression(m)
        except MetricError:
            expression = None
        return {"name": m["name"], "label": m["label"], "description": m["description"], "table": m["table"],
                "measure": m["measure"], "expression": expression, "filters": m["filters"],
                "time_column": m["time_column"], "dimensions": m["dimensions"], "certified": m["certified"],
                "version": m["version"]}

    def _agents(self, share_id: str) -> list[str]:
        with self.engine.connect() as conn:
            return [r.app for r in conn.execute(select(share_agents.c.app).where(share_agents.c.share_id == share_id)
                                                .order_by(share_agents.c.app))]

    def publish(self, actor: str, everyone: bool, ref: str, body: Any) -> dict:
        if not isinstance(body, dict) or set(body) - {"metrics", "sample_questions", "notes"}:
            raise ShareError("a package is {metrics: [names], sample_questions: [...], notes: ...}")
        row = self.shares._find(ref, actor, everyone)
        if not self.shares._active(row):
            raise ShareError("the share has ended", 409)
        names = body.get("metrics") or []
        questions = body.get("sample_questions") or []
        notes = body.get("notes") or ""
        if not isinstance(names, list) or len(names) > 100 or not all(isinstance(n, str) for n in names):
            raise ShareError("metrics: up to 100 metric names")
        if not isinstance(questions, list) or len(questions) > 50 or not all(
                isinstance(q, str) and 3 <= len(q) <= 500 for q in questions):
            raise ShareError("sample_questions: up to 50 questions of 3 to 500 characters")
        if not isinstance(notes, str) or len(notes) > 20_000:
            raise ShareError("notes: text, up to 20,000 characters")
        tables = json.loads(row["tables"])
        shared = {t.lower() for t in tables}
        terms: dict[str, dict] = {}
        described = [self._table(t, terms) for t in tables]
        metrics = [self._metric(n, shared) for n in sorted(set(names))]
        glossary = []
        for t in sorted(terms):
            info = self._aspect(t, "glossaryTermInfo")
            glossary.append({"urn": t, "name": info.get("name") or t.rsplit(":", 1)[-1],
                             "definition": info.get("definition") or ""})
        with self.engine.connect() as conn:
            last = conn.execute(select(share_packages.c.version).where(share_packages.c.share_id == row["id"])
                                .order_by(share_packages.c.version.desc()).limit(1)).scalar() or 0
        version = int(last) + 1
        manifest = {
            "kind": "cairn.data-package", "format": 1, "version": version,
            "share": {"id": row["id"], "name": row["name"], "recipient": row["recipient"],
                      "expires_at": row["expires_s"]},
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "published_by": actor,
            "catalog": {"uri": self.shares.catalog_url, "oauth2-server-uri": self.shares.token_url},
            "tables": described, "glossary": glossary, "metrics": metrics,
            "sample_questions": questions, "notes": notes,
            "agents": [{"name": a, "invoke": f"/v1/shares/recipient/agents/{a}/invocations"}
                       for a in self._agents(row["id"])]}
        signature = self.shares.tokens.sign_document(manifest, TYP)
        digest = hashlib.sha256(canonical(manifest)).hexdigest()
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(insert(share_packages).values(
                    share_id=row["id"], version=version, manifest=canonical(manifest).decode(), signature=signature,
                    digest=digest, created_by=actor, created_ms=_now()))
                if self.audit is not None:
                    self.audit.record("shares", "share.package_published", actor, None,
                                      {"share": row["id"], "name": row["name"], "version": version, "digest": digest,
                                       "tables": len(described), "metrics": [m["name"] for m in metrics]}, conn=conn)
        except IntegrityError:
            raise ShareError("another version was published at the same time; publish again", 409)
        return {"version": version, "digest": digest, "manifest": manifest, "signature": signature}

    def _latest(self, share_id: str, version: Optional[int] = None) -> Optional[dict]:
        q = select(share_packages).where(share_packages.c.share_id == share_id)
        q = q.where(share_packages.c.version == version) if version else \
            q.order_by(share_packages.c.version.desc()).limit(1)
        with self.engine.connect() as conn:
            r = conn.execute(q).first()
        if r is None:
            return None
        return {"version": r.version, "digest": r.digest, "manifest": json.loads(r.manifest),
                "signature": r.signature, "published_by": r.created_by, "published_ms": r.created_ms}

    def package(self, actor: str, everyone: bool, ref: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        with self.engine.connect() as conn:
            versions = [{"version": r.version, "digest": r.digest, "published_by": r.created_by,
                         "published_ms": r.created_ms}
                        for r in conn.execute(select(share_packages).where(share_packages.c.share_id == row["id"])
                                              .order_by(share_packages.c.version.desc()))]
        return {"share": row["name"], "latest": self._latest(row["id"]), "versions": versions}

    def list(self, actor: str, everyone: bool) -> list[dict]:
        """Every share the caller sees, with its package's latest version and the agents attached."""
        out = []
        for sh in self.shares.list(actor, everyone):
            latest = self._latest(sh["id"])
            m = (latest or {}).get("manifest") or {}
            out.append({"share": sh["name"], "share_id": sh["id"], "recipient": sh["recipient"], "state": sh["state"],
                        "version": (latest or {}).get("version"), "digest": (latest or {}).get("digest"),
                        "published_ms": (latest or {}).get("published_ms"),
                        "tables": len(m.get("tables") or sh["tables"]),
                        "metrics": [x["name"] for x in m.get("metrics") or []],
                        "agents": self._agents(sh["id"])})
        return out

    def attach(self, actor: str, everyone: bool, ref: str, app: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        if not self.shares._active(row):
            raise ShareError("the share has ended", 409)
        if self.agent_apps is None:
            raise ShareError("agent apps are not configured", 503)
        from cairn.ai.agent_apps import AgentAppError

        try:
            info = self.agent_apps.get(actor, str(app or ""))
        except AgentAppError:
            raise ShareError(f"no agent app {app} that you may use", 404)
        if "model" not in (info.get("scopes") or []):
            raise ShareError(f"{app} has no model scope: an agent on shared data answers through Cairn's AI gateway",
                             409)
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(insert(share_agents).values(share_id=row["id"], app=info["name"], attached_by=actor,
                                                         attached_ms=_now()))
                if self.audit is not None:
                    self.audit.record("shares", "share.agent_attached", actor, None,
                                      {"share": row["id"], "name": row["name"], "app": info["name"]}, conn=conn)
        except IntegrityError:
            raise ShareError(f"{app} is already attached to this share", 409)
        return self.agents(actor, everyone, ref)

    def detach(self, actor: str, everyone: bool, ref: str, app: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        with self.engine.begin() as conn:
            n = conn.execute(share_agents.delete().where((share_agents.c.share_id == row["id"])
                                                         & (share_agents.c.app == app))).rowcount
            if not n:
                raise ShareError(f"{app} is not attached to this share", 404)
            if self.audit is not None:
                self.audit.record("shares", "share.agent_detached", actor, None,
                                  {"share": row["id"], "name": row["name"], "app": app}, conn=conn)
        return self.agents(actor, everyone, ref)

    def agents(self, actor: str, everyone: bool, ref: str, days: int = 30) -> dict:
        """The agents attached to a share, with the recipient's use of each over the last ``days`` days."""
        row = self.shares._find(ref, actor, everyone)
        since = _now() - max(1, min(int(days), 400)) * 86_400_000
        with self.engine.connect() as conn:
            attached = [dict(r._mapping) for r in conn.execute(select(share_agents).where(
                share_agents.c.share_id == row["id"]).order_by(share_agents.c.app))]
            runs = [dict(r._mapping) for r in conn.execute(select(share_agent_runs).where(
                (share_agent_runs.c.share_id == row["id"]) & (share_agent_runs.c.started_ms >= since)))]
        out = []
        for a in attached:
            mine = [r for r in runs if r["app"] == a["app"]]
            out.append({"app": a["app"], "attached_by": a["attached_by"], "attached_ms": a["attached_ms"],
                        "usage": {"runs": len(mine), "failures": sum(1 for r in mine if not r["ok"]),
                                  "ms": sum(int(r["ms"]) for r in mine),
                                  "sessions": len({r["session_id"] for r in mine}),
                                  "last_ms": max([int(r["started_ms"]) for r in mine] or [0]) or None}})
        return {"share": row["name"], "recipient": row["recipient"], "recipient_urn": recipient_urn(row["id"]),
                "days": days, "agents": out}

    # ---- the recipient's side -----------------------------------------------------------------------
    def recipient(self, authorization: str) -> dict:
        """The active share behind a recipient's credential: the share's OAuth token (Bearer) or its credential
        (HTTP Basic, id and secret). Anything else, or a share that has ended, is refused."""
        sid = ""
        if authorization[:7].lower() == "bearer ":
            try:
                claims = self.shares.tokens.verify(authorization[7:].strip(), AUDIENCE)
            except ValueError:
                raise ShareError("not a share token", 401)
            sub = str(claims.get("sub") or "")
            if not sub.startswith(PREFIX) or claims.get("share") != sub.removeprefix(PREFIX):
                raise ShareError("not a share token", 401)
            sid = sub.removeprefix(PREFIX)
            with self.engine.connect() as conn:
                row = self.shares._row(conn, sid)
        elif authorization[:6].lower() == "basic ":
            try:
                sid, _, secret = base64.b64decode(authorization[6:].strip()).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                raise ShareError("malformed credential", 401)
            with self.engine.connect() as conn:
                row = self.shares._row(conn, sid) if sid else None
            if row is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
                raise ShareError("unknown share credential", 401)
        else:
            raise ShareError("the share's credential is needed (its OAuth token, or the credential as Basic)", 401)
        if row is None:
            raise ShareError("unknown share", 401)
        if not self.shares._active(row):
            self.shares._count(row["id"], "", refusals=1)
            raise ShareError("the share has ended", 403)
        return row

    def recipient_package(self, row: dict, version: Optional[int] = None) -> dict:
        got = self._latest(row["id"], version)
        if got is None:
            raise ShareError("this share has no data package" + (f" version {version}" if version else ""), 404)
        self.shares._count(row["id"], "", requests=1)
        if self.audit is not None:
            self.audit.record("shares", "share.package_fetched", recipient_urn(row["id"]), None,
                              {"share": row["id"], "version": got["version"]})
        return {"version": got["version"], "digest": got["digest"], "manifest": got["manifest"],
                "signature": got["signature"], "jwks_uri": "/v1/trino/jwks.json", "typ": TYP}

    def recipient_agents(self, row: dict) -> list[dict]:
        out = []
        for name in self._agents(row["id"]):
            try:
                d = self.agent_apps._row(name) if self.agent_apps is not None else None
            except Exception:  # noqa: BLE001 - an app deleted since: not offered
                d = None
            if d is not None:
                out.append({"name": name, "description": d["manifest"].get("description", ""),
                            "status": d["status"], "invoke": f"/v1/shares/recipient/agents/{name}/invocations"})
        return out

    def invoke(self, row: dict, app: str, body: Any) -> dict:
        if self.agent_apps is None:
            raise ShareError("agent apps are not configured", 503)
        if not isinstance(body, dict):
            raise ShareError("an invocation is {input, session_id}")
        with self.engine.connect() as conn:
            att = conn.execute(select(share_agents).where((share_agents.c.share_id == row["id"])
                                                          & (share_agents.c.app == app))).first()
        if att is None:
            raise ShareError("no such agent on this share", 404)
        ttl = max(60, min(900, int(row["expires_s"]) - int(time.time())))
        lake = self.shares.tokens.mint(recipient_urn(row["id"]), ttl, audience=AUDIENCE,
                                       extra={"share": row["id"], "agent_app": app})
        lakehouse = {"uri": self.shares.catalog_url, "token": lake["token"], "tables": json.loads(row["tables"])}
        from cairn.ai.agent_apps import AgentAppError

        started, ok, out, error = _now(), False, None, None
        text = body.get("input") if isinstance(body.get("input"), str) else ""
        try:
            out = self.agent_apps.invoke_as(recipient_urn(row["id"]), app,
                                            {"input": body.get("input"), "session_id": body.get("session_id")},
                                            identity="share-recipient", sponsor=att.attached_by, scopes=["model"],
                                            lakehouse=lakehouse)
            ok = True
        except AgentAppError as e:
            error = e
        ms = _now() - started
        with self.engine.begin() as conn:
            conn.execute(insert(share_agent_runs).values(
                id=uuid.uuid4().hex, share_id=row["id"], app=app,
                session_id=str((out or {}).get("session_id") or body.get("session_id") or "")[:128], ok=ok, ms=ms,
                input_chars=len(text), output_chars=len((out or {}).get("output") or ""), started_ms=started))
            if self.audit is not None:
                self.audit.record("shares", "share.agent_invoked", recipient_urn(row["id"]), None,
                                  {"share": row["id"], "name": row["name"], "recipient": row["recipient"], "app": app,
                                   "identity": "share-recipient", "sponsor": att.attached_by, "ok": ok, "ms": ms},
                                  conn=conn)
        if error is not None:
            raise ShareError(str(error), error.status)
        return out


def build_share_packages_router(pk: SharePackages, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are managed by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    def recipient(request: Request) -> dict:
        try:
            return pk.recipient(request.headers.get("authorization", ""))
        except ShareError as e:
            headers = {"WWW-Authenticate": 'Bearer realm="shares"'} if e.status == 401 else None
            raise HTTPException(e.status, str(e), headers=headers)

    # The recipient's endpoints come first: "recipient" is not a share's name.
    @router.get("/recipient/package")
    def recipient_package(request: Request, version: Optional[int] = None):
        """The share's data package, signed: for the recipient, with the share's credential."""
        return call(pk.recipient_package, recipient(request), version)

    @router.get("/recipient/agents")
    def recipient_agents(request: Request):
        return {"agents": call(pk.recipient_agents, recipient(request))}

    @router.post("/recipient/agents/{app}/invocations")
    def recipient_invoke(app: str, request: Request, body: dict = Body(...)):
        """Asks an agent the provider attached to the share; it reads only the shared tables."""
        return call(pk.invoke, recipient(request), app, body)

    @router.put("/{ref}/package")
    def publish(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        """Publishes a new version of the share's data package (signed)."""
        return call(pk.publish, person(p).urn, operator(p), ref, body)

    @router.get("/{ref}/package")
    def package(ref: str, p=Depends(current_principal)):
        return call(pk.package, p.urn, operator(p), ref)

    @router.get("/{ref}/agents")
    def agents(ref: str, days: int = 30, p=Depends(current_principal)):
        return call(pk.agents, p.urn, operator(p), ref, days)

    @router.post("/{ref}/agents")
    def attach(ref: str, app: str = Body(..., embed=True), p=Depends(current_principal)):
        return call(pk.attach, person(p).urn, operator(p), ref, app)

    @router.delete("/{ref}/agents/{app}")
    def detach(ref: str, app: str, p=Depends(current_principal)):
        return call(pk.detach, person(p).urn, operator(p), ref, app)

    # A list of its own (/v1/shares/<anything> is a share's name).
    listing = APIRouter(prefix="/v1/share-packages", tags=["shares"])

    @listing.get("")
    def list_(p=Depends(current_principal)):
        """The caller's shares (an operator: all) with their data package's latest version and attached agents."""
        return {"packages": pk.list(p.urn, operator(p))}

    both = APIRouter()
    both.include_router(router)
    both.include_router(listing)
    return both
