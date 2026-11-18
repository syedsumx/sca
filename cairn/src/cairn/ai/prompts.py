"""A prompt registry: prompts kept in Cairn with versions and aliases, fetched by agents at run time (L171).

An agent's instructions change more often than its code, and a change to them is the commonest reason an agent gets
worse. Kept here, a prompt has a history, a way back, and a name for "the one in production".

* **Prompts** (``/v1/prompts``): ``POST`` with ``{"name", "template", "description"}`` makes one at version 1.
  A template is text with ``{{variable}}`` places. ``PUT /v1/prompts/<name>`` with a different template makes the
  next version: versions are never changed or removed while the prompt exists.
* **Aliases** name a version: ``PUT /v1/prompts/<name>/aliases/production`` with ``{"version": 3}``. Moving an alias
  is how a prompt is released, and moving it back is the rollback; both are audited with who and from which version.
* **Fetching**: ``GET /v1/prompts/<name>`` (``?version=`` or ``?alias=``; by default the ``production`` alias, and
  the latest version when there is none). ``POST /v1/prompts/<name>/render`` fills the variables: every one must be
  given, and a value is inserted as it is, never read again as a template. In an agent: ``ctx.prompt("support",
  customer="Ana")``.
* **Who.** Making a prompt needs ``BUILD_AGENTS``; its owner (or an operator) changes it and moves its aliases.
  Reading one needs to be its owner, an operator, or allowed ``USE_PROMPT`` on ``urn:li:cairnPrompt:<name>``: an
  agent app fetches as the person it runs for, so grant that to the people who use the agent. Prompts are changed by
  people, never by an agent's credential.

Audited: ``prompt.created``, ``prompt.version_added``, ``prompt.alias_set``, ``prompt.alias_removed``,
``prompt.deleted`` (versions and names, never the text).
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, Text, and_, delete, func, insert, select

from cairn.common.db import metadata

NAME = re.compile(r"^[a-z][a-z0-9_-]{1,62}[a-z0-9]$")
ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
MAX_TEMPLATE = 200_000
DEFAULT_ALIAS = "production"
PRIVILEGE, USE = "BUILD_AGENTS", "USE_PROMPT"

prompts = Table(
    "cairn_prompts", metadata,
    Column("name", String(64), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("description", String(2000), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
prompt_versions = Table(
    "cairn_prompt_versions", metadata,
    Column("name", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("template", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("note", String(500), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("name", "version"),
)
prompt_aliases = Table(
    "cairn_prompt_aliases", metadata,
    Column("name", String(64), nullable=False),
    Column("alias", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("set_by", String(256), nullable=False),
    Column("set_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("name", "alias"),
)


class PromptError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:cairnPrompt:{name}"


def variables(template: str) -> list[str]:
    seen: list[str] = []
    for v in VARIABLE.findall(template):
        if v not in seen:
            seen.append(v)
    return seen


def render(template: str, values: dict) -> str:
    """The template with its variables filled. Every variable needs a value; a value is text put in as it is (a
    value holding ``{{x}}`` stays that text)."""
    need = variables(template)
    values = values if isinstance(values, dict) else {}
    missing = [v for v in need if v not in values]
    if missing:
        raise PromptError("no value for: " + ", ".join(missing))
    extra = sorted(set(values) - set(need))
    if extra:
        raise PromptError("not variables of this prompt: " + ", ".join(extra)[:300])
    return VARIABLE.sub(lambda m: str(values[m.group(1)]), template)


def _template(body: dict) -> str:
    t = body.get("template")
    if not isinstance(t, str) or not t.strip() or len(t) > MAX_TEMPLATE:
        raise PromptError(f"template: text, at most {MAX_TEMPLATE} characters")
    return t


class Prompts:
    def __init__(self, engine, allowed: Callable[[str, str, Optional[str]], bool], audit=None):
        """``allowed(actor, privilege, urn)``: Cairn's policy decision."""
        self.engine, self.allowed, self.audit = engine, allowed, audit

    def _operator(self, actor: str) -> bool:
        return self.allowed(actor, "MANAGE_SYSTEM_OPERATIONS", None)

    def _row(self, name: str, actor: str, manage: bool = False) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(prompts).where(prompts.c.name == name)).first()
        mine = r is not None and (r.owner == actor or self._operator(actor))
        if r is None or not (mine or self.allowed(actor, USE, urn(name))):
            raise PromptError("no such prompt", 404)
        if manage and not mine:
            raise PromptError("only the prompt's owner or an operator changes it", 403)
        return dict(r._mapping)

    def _versions(self, name: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(prompt_versions).where(prompt_versions.c.name == name).order_by(prompt_versions.c.version))]

    def _aliases(self, name: str) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {r.alias: r.version for r in conn.execute(select(prompt_aliases).where(prompt_aliases.c.name == name))}

    def _public(self, row: dict, v: dict) -> dict:
        aliases = self._aliases(row["name"])
        return {"name": row["name"], "owner": row["owner"], "description": row["description"], "version": v["version"],
                "template": v["template"], "variables": variables(v["template"]), "sha256": v["sha256"],
                "note": v["note"], "created_by": v["created_by"], "created_ms": v["created_ms"],
                "aliases": [{"alias": a, "version": n} for a, n in sorted(aliases.items())],
                "latest": max(x["version"] for x in self._versions(row["name"]))}

    def _add(self, conn, name: str, version: int, template: str, note: str, actor: str) -> None:
        conn.execute(insert(prompt_versions).values(
            name=name, version=version, template=template, sha256=hashlib.sha256(template.encode()).hexdigest(),
            note=str(note or "")[:500], created_by=actor, created_ms=int(time.time() * 1000)))

    # ---- prompts and versions ----------------------------------------------------------------------------
    def create(self, actor: str, body: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        if not self.allowed(actor, PRIVILEGE, None):
            raise PromptError(f"making prompts needs {PRIVILEGE}", 403)
        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise PromptError("name: 3 to 64 lowercase letters, digits, - and _")
        template = _template(body)
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(prompts).values(name=name, owner=actor, description=str(body.get("description") or "")[:2000],
                                                    created_ms=int(time.time() * 1000)))
                self._add(conn, name, 1, template, body.get("note") or "", actor)
        except IntegrityError:
            raise PromptError(f"a prompt {name} exists", 409)
        self._audit("prompt.created", actor, name, {"version": 1})
        return self.get(actor, name, version=1)

    def update(self, actor: str, name: str, body: dict) -> dict:
        """Declares the prompt's text and description: a different template is the next version."""
        row = self._row(name, actor, manage=True)
        latest = self._versions(name)[-1]
        with self.engine.begin() as conn:
            if "description" in body and str(body["description"] or "")[:2000] != row["description"]:
                conn.execute(prompts.update().where(prompts.c.name == name).values(description=str(body["description"] or "")[:2000]))
            if "template" in body and _template(body) != latest["template"]:
                n = int(conn.execute(select(func.max(prompt_versions.c.version)).where(prompt_versions.c.name == name)).scalar()) + 1
                self._add(conn, name, n, body["template"], body.get("note") or "", actor)
                added = n
            else:
                added = None
        if added:
            self._audit("prompt.version_added", actor, name, {"version": added})
        return self.get(actor, name, version=added or latest["version"])

    def get(self, actor: str, name: str, version: Optional[int] = None, alias: Optional[str] = None) -> dict:
        row = self._row(name, actor)
        versions = {v["version"]: v for v in self._versions(name)}
        if version is not None and alias:
            raise PromptError("version or alias, not both")
        if version is None:
            aliases = self._aliases(name)
            if alias:
                if alias not in aliases:
                    raise PromptError(f"the prompt has no alias {alias}", 404)
                version = aliases[alias]
            else:
                version = aliases.get(DEFAULT_ALIAS, max(versions))
        if int(version) not in versions:
            raise PromptError(f"the prompt has no version {version}", 404)
        return self._public(row, versions[int(version)])

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r.name for r in conn.execute(select(prompts.c.name).order_by(prompts.c.name))]
        out = []
        for n in names:
            try:
                p = self.get(actor, n)
            except PromptError:
                continue
            out.append({k: p[k] for k in ("name", "owner", "description", "template", "version", "latest", "aliases", "variables")})
        return out

    def versions(self, actor: str, name: str) -> list[dict]:
        self._row(name, actor)
        return [{"version": v["version"], "sha256": v["sha256"], "note": v["note"], "created_by": v["created_by"],
                 "created_ms": v["created_ms"], "variables": variables(v["template"]), "chars": len(v["template"])}
                for v in self._versions(name)]

    def delete(self, actor: str, name: str) -> None:
        self._row(name, actor, manage=True)
        with self.engine.begin() as conn:
            for t in (prompt_aliases, prompt_versions):
                conn.execute(delete(t).where(t.c.name == name))
            conn.execute(delete(prompts).where(prompts.c.name == name))
        self._audit("prompt.deleted", actor, name, {})

    def render(self, actor: str, name: str, body: dict) -> dict:
        p = self.get(actor, name, body.get("version"), body.get("alias"))
        return {"name": name, "version": p["version"], "text": render(p["template"], body.get("values") or {})}

    # ---- aliases -----------------------------------------------------------------------------------------
    def set_alias(self, actor: str, name: str, alias: str, version: Any) -> dict:
        self._row(name, actor, manage=True)
        if not ALIAS.match(alias or ""):
            raise PromptError("alias: lowercase letters, digits, - and _ (at most 32)")
        if isinstance(version, bool) or not isinstance(version, int) or version not in {v["version"] for v in self._versions(name)}:
            raise PromptError("version: one of the prompt's versions")
        was = self._aliases(name).get(alias)
        with self.engine.begin() as conn:
            conn.execute(delete(prompt_aliases).where(and_(prompt_aliases.c.name == name, prompt_aliases.c.alias == alias)))
            conn.execute(insert(prompt_aliases).values(name=name, alias=alias, version=version, set_by=actor,
                                                       set_ms=int(time.time() * 1000)))
        self._audit("prompt.alias_set", actor, name, {"alias": alias, "version": version, "was": was})
        return self.get(actor, name, alias=alias)

    def remove_alias(self, actor: str, name: str, alias: str) -> None:
        self._row(name, actor, manage=True)
        with self.engine.begin() as conn:
            if not conn.execute(delete(prompt_aliases).where(and_(prompt_aliases.c.name == name,
                                                                  prompt_aliases.c.alias == alias))).rowcount:
                raise PromptError(f"the prompt has no alias {alias}", 404)
        self._audit("prompt.alias_removed", actor, name, {"alias": alias})

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ai", action, actor, urn(name), details)


def build_prompts_router(ps: Prompts, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/prompts", tags=["prompts"])

    def person(p) -> str:
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "prompts are changed by people")
        return p.urn

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except PromptError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(p=Depends(current_principal)):
        return call(ps.list, p.urn)

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        return call(ps.create, person(p), body)

    @router.get("/{name}")
    def get(name: str, version: Optional[int] = None, alias: Optional[str] = None, p=Depends(current_principal)):
        """A prompt's version: the one asked for, else the production alias, else the latest."""
        return call(ps.get, p.urn, name, version, alias)

    @router.put("/{name}")
    def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return call(ps.update, person(p), name, body)

    @router.delete("/{name}", status_code=204)
    def remove(name: str, p=Depends(current_principal)):
        call(ps.delete, person(p), name)

    @router.get("/{name}/versions")
    def versions(name: str, p=Depends(current_principal)):
        return call(ps.versions, p.urn, name)

    @router.post("/{name}/render")
    def render_(name: str, body: dict = Body(default={}), p=Depends(current_principal)):
        """The prompt with its variables filled (every variable needs a value)."""
        return call(ps.render, p.urn, name, body or {})

    @router.put("/{name}/aliases/{alias}")
    def set_alias(name: str, alias: str, version: int = Body(..., embed=True), p=Depends(current_principal)):
        return call(ps.set_alias, person(p), name, alias, version)

    @router.delete("/{name}/aliases/{alias}", status_code=204)
    def remove_alias(name: str, alias: str, p=Depends(current_principal)):
        call(ps.remove_alias, person(p), name, alias)

    return router
