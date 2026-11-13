"""Project memory and learned rules (L164): what the AI coding agents working in a repository remember across
sessions, kept in Cairn's governed agent memory instead of in files on one person's machine.

* A **project** is a memory space with a name (the repository's, usually), an owner and optionally a team (a
  group). Its members are the owner and the team's members; nobody else can see that it exists.
* It holds two kinds of memory. **Notes** are facts about the project ("the orders table is rebuilt nightly",
  "CI needs the store built first"). **Rules** are how to work here ("qualify tables with their schema").
* **Who writes what** follows agent memory's scopes (:mod:`cairn.ai.agent_memory`), with the project in the place
  of the agent:

  - an agent (an IDE's connection to Cairn's MCP server, acting for the person signed in) writes only for that
    person: its notes, and rules it **proposes** after being corrected. Nobody else recalls them;
  - a person **promotes** a proposed rule, or writes one themselves, for the team (any member) or, in a project
    without a team, for the project (its owner). Only what a person promoted or wrote is shared.

* **Recall** is by meaning and recency, as for agent memory: a member recalls their own memories and the shared
  ones, never another person's.
* **Governed like agent memory**, because it is agent memory (the same table, checks, limits and audit): content
  holding personal data, a credential or text that reads like instructions to an AI is refused; memories not
  recalled for the retention period expire; a person sees and deletes theirs under ``/v1/me/agent-memories``.
* ``cairn agents memory sync`` writes the project's shared rules into the repository's ``AGENTS.md`` (Cairn's own
  section), so every tool reads them and the change is reviewed like any other.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, delete, insert, select, update

from cairn.ai.agent_memory import AgentMemory, AgentMemoryError, agent_memories
from cairn.common.db import metadata

agent_projects = Table(
    "cairn_agent_projects", metadata,
    Column("id", String(24), primary_key=True),               # prj_…
    Column("name", String(40), nullable=False, unique=True),
    Column("description", String(500), nullable=False, default=""),
    Column("team", String(512), nullable=True),               # a group urn: its members share the project
    Column("owner", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

KINDS = ("note", "rule")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
RULES_BEGIN, RULES_END = "<!-- cairn:rules:begin -->", "<!-- cairn:rules:end -->"


class ProjectError(AgentMemoryError):
    pass


def _space(project_id: str, kind: str) -> str:
    """Where a project's memories of a kind live in agent memory (its ``agent`` column)."""
    return project_id if kind == "note" else f"{project_id}:rules"


class _Projects:
    """The projects, shaped as agent memory expects its agents (``get`` by the space's id)."""

    def __init__(self, engine):
        self.engine = engine

    def get(self, space: str) -> dict:
        from cairn.identity.agents import AgentError

        with self.engine.connect() as c:
            r = c.execute(select(agent_projects).where(agent_projects.c.id == space.split(":", 1)[0])).first()
        if r is None:
            raise AgentError("no such project", 404)
        return {"id": space, "name": r.name, "owner": r.owner, "kind": "project", "team": r.team}

    def list(self) -> list[dict]:
        return []


class ProjectMemory:
    def __init__(self, engine, embed: Callable[[str], list[float]], is_operator: Callable[[str], bool],
                 groups_of: Callable[[str], list[str]], audit=None, clock=time.time):
        self.engine, self.is_operator, self.groups_of, self.audit, self.clock = engine, is_operator, groups_of, audit, clock
        self.memory = AgentMemory(engine, _Projects(engine), embed, lambda actor, a: True, is_operator, groups_of,
                                  audit, clock)

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("agents", action, actor, name, details)

    # ---- projects ----------------------------------------------------------------------------------------------
    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "name": r.name, "description": r.description, "team": r.team, "owner": r.owner,
                "created_ms": r.created_ms}

    def _member(self, actor: str, r) -> bool:
        return r.owner == actor or (bool(r.team) and r.team in self.groups_of(actor)) or self.is_operator(actor)

    def _row(self, actor: str, name: str):
        """The project, for a member. Anyone else is told it does not exist."""
        with self.engine.connect() as c:
            r = c.execute(select(agent_projects).where(agent_projects.c.name == str(name))).first()
        if r is None or not self._member(actor, r):
            raise ProjectError("no such project", 404)
        return r

    def _team(self, actor: str, team: Any) -> Optional[str]:
        if team in (None, ""):
            return None
        if not isinstance(team, str) or not team.startswith("urn:li:corpGroup:"):
            raise ProjectError("team: a group (urn:li:corpGroup:…)", 422)
        if team not in self.groups_of(actor) and not self.is_operator(actor):
            raise ProjectError("team: a group you are a member of", 403)
        return team

    def create(self, actor: str, name: str, description: str = "", team: Any = None) -> dict:
        if not isinstance(name, str) or not NAME.match(name):
            raise ProjectError("name: lower-case letters, digits and hyphens, 2 to 40, starting with a letter", 422)
        team = self._team(actor, team)
        pid = f"prj_{uuid.uuid4().hex[:16]}"
        with self.engine.begin() as c:
            if c.execute(select(agent_projects.c.id).where(agent_projects.c.name == name)).first() is not None:
                raise ProjectError(f"a project called {name} exists (if it is yours or your team's, use it)", 409)
            c.execute(insert(agent_projects).values(id=pid, name=name, description=str(description or "")[:500],
                                                    team=team, owner=actor, created_ms=int(self.clock() * 1000)))
        self._audit("agent_project.created", actor, name, {"team": team})
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        return self._public(self._row(actor, name))

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(agent_projects).order_by(agent_projects.c.name)).all()
        return [self._public(r) for r in rows if self._member(actor, r)]

    def _manage(self, actor: str, name: str):
        r = self._row(actor, name)
        if r.owner != actor and not self.is_operator(actor):
            raise ProjectError("only the project's owner or an operator changes or deletes it", 403)
        return r

    def update(self, actor: str, name: str, description: Any = None, team: Any = "") -> dict:
        r = self._manage(actor, name)
        values: dict[str, Any] = {}
        if description is not None:
            values["description"] = str(description)[:500]
        if team != "":                                          # None removes the team; "" leaves it
            new = self._team(actor, team)
            if new != r.team:
                values["team"] = new
                # The old team's shared memories do not follow the project to another team.
                with self.engine.begin() as c:
                    c.execute(delete(agent_memories).where(
                        agent_memories.c.agent.in_([_space(r.id, k) for k in KINDS]), agent_memories.c.scope == "team"))
        if values:
            with self.engine.begin() as c:
                c.execute(update(agent_projects).where(agent_projects.c.id == r.id).values(**values))
            self._audit("agent_project.updated", actor, name, {"fields": sorted(values)})
        return self.get(actor, name)

    def delete(self, actor: str, name: str) -> None:
        r = self._manage(actor, name)
        for kind in KINDS:
            self.memory.drop_agent(_space(r.id, kind))
        with self.engine.begin() as c:
            c.execute(delete(agent_projects).where(agent_projects.c.id == r.id))
        self._audit("agent_project.deleted", actor, name, {})

    # ---- memories ----------------------------------------------------------------------------------------------
    @staticmethod
    def _kind(kind: Any) -> str:
        if kind not in KINDS:
            raise ProjectError(f"kind: one of {', '.join(KINDS)}", 422)
        return kind

    def _shape(self, m: dict, project) -> dict:
        kind = "rule" if str(m["agent"]).endswith(":rules") else "note"
        out = {k: v for k, v in m.items() if k not in ("agent", "source_run")}
        out.update(kind=kind, project=project.name,
                   status="proposed" if kind == "rule" and m["scope"] == "person" else "shared" if m["scope"] != "person"
                   else "mine")
        return out

    def remember(self, actor: str, name: str, content: Any, kind: Any = "note", shared: bool = False,
                 by_agent: bool = False) -> dict:
        """A note or a rule. ``shared``: for the project's members (a person's own decision, never an agent's)."""
        r = self._row(actor, name)
        kind = self._kind(kind)
        if shared and by_agent:
            raise ProjectError("an agent remembers only for the person it works for: a person shares or promotes it", 403)
        scope, team = "person", None
        if shared:
            scope, team = ("team", r.team) if r.team else ("shared", None)
        m = self.memory.remember(actor, _space(r.id, kind), content, scope=scope, team=team,
                                 source="agent" if by_agent else "person")
        return self._shape(m, r)

    def _visible(self, actor: str, r, kind: Optional[str]) -> list:
        spaces = [_space(r.id, k) for k in ((kind,) if kind else KINDS)]
        out = []
        with self.engine.connect() as c:
            for space in spaces:
                out += c.execute(select(agent_memories).where(self.memory._visible_where(actor, space))
                                 .order_by(agent_memories.c.created_ms)).all()
        return out

    def memories(self, actor: str, name: str, kind: Optional[str] = None) -> list[dict]:
        r = self._row(actor, name)
        kind = self._kind(kind) if kind else None
        return [self._shape(self.memory._public(m, deletable=self.memory._may_delete(actor, m)), r)
                for m in self._visible(actor, r, kind)]

    def recall(self, actor: str, name: str, query: Any = "", k: Any = 5, kind: Optional[str] = None) -> list[dict]:
        r = self._row(actor, name)
        kind = self._kind(kind) if kind else None
        found: list[dict] = []
        for one in ((kind,) if kind else KINDS):
            found += self.memory.recall(actor, _space(r.id, one), query, k)
        found.sort(key=lambda m: (-m["score"], -m["created_ms"]))
        return [self._shape(m, r) for m in found[:k]]

    def _mine_in(self, actor: str, r, mid: str):
        got = [m for m in self._visible(actor, r, None) if m.id == mid]
        if not got:
            raise ProjectError("no such memory", 404)
        return got[0]

    def promote(self, actor: str, name: str, mid: str) -> dict:
        """A person makes their proposed rule the project's: shared with its members from now on."""
        r = self._row(actor, name)
        m = self._mine_in(actor, r, mid)
        if not str(m.agent).endswith(":rules") or m.scope != "person" or m.subject != actor:
            raise ProjectError("only your own proposed rule can be promoted", 409)
        shared = self.remember(actor, name, m.content, "rule", shared=True)
        with self.engine.begin() as c:
            c.execute(delete(agent_memories).where(agent_memories.c.id == mid))
        self._audit("agent_project.rule_promoted", actor, name, {"from": mid, "to": shared["id"]})
        return shared

    def forget(self, actor: str, name: str, mid: str) -> None:
        r = self._row(actor, name)
        m = self._mine_in(actor, r, mid)
        if not self.memory._may_delete(actor, m):
            raise ProjectError("it is the project's: its owner, an operator or a member of its team deletes it", 403)
        with self.engine.begin() as c:
            c.execute(delete(agent_memories).where(agent_memories.c.id == mid))
        self._audit("agent_memory.deleted", actor, m.agent, {"id": mid, "by": "person", "project": name})

    def rules(self, actor: str, name: str) -> list[dict]:
        """The project's shared rules, oldest first (what ``cairn agents memory sync`` writes into the repository)."""
        return [m for m in self.memories(actor, name, "rule") if m["status"] == "shared"]


def rules_block(project: str, rules: list[dict]) -> str:
    lines = [RULES_BEGIN, f"## Learned rules ({project})", "",
             "Kept in Cairn's project memory; change them there (`cairn agents memory`), then `cairn agents memory "
             "sync`.", ""]
    lines += [f"- {' '.join(str(r['content']).split())}" for r in rules] or ["- (none yet)"]
    return "\n".join(lines + [RULES_END]) + "\n"


def merge_rules(existing: Optional[str], block: str) -> str:
    """``AGENTS.md`` with the rules section replaced (or added at the end); everything else as it was."""
    text = existing or ""
    has_begin, has_end = RULES_BEGIN in text, RULES_END in text
    if has_begin != has_end or (has_begin and text.index(RULES_BEGIN) > text.index(RULES_END)):
        raise ValueError(f"AGENTS.md has one of the rules markers without the other ({RULES_BEGIN} ... {RULES_END})")
    if has_begin:
        start, end = text.index(RULES_BEGIN), text.index(RULES_END) + len(RULES_END)
        return text[:start] + block.rstrip("\n") + text[end:]
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block


class ProjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    description: str = Field(default="", max_length=500)
    team: Optional[str] = None


class ProjectPatch(BaseModel):
    description: Optional[str] = Field(default=None, max_length=500)
    team: Optional[str] = ""                                    # null removes the team; left out, it stays


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    kind: str = "note"
    shared: bool = False


class RecallIn(BaseModel):
    query: str = Field(default="", max_length=4_000)
    k: int = Field(default=5, ge=1, le=50)
    kind: Optional[str] = None


def build_router(pm: ProjectMemory, current_principal) -> Any:
    from fastapi import APIRouter, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/agent-projects", tags=["ai"])

    async def call(fn, *a, **k):
        try:
            return await run_in_threadpool(fn, *a, **k)
        except AgentMemoryError as e:
            raise HTTPException(e.status, str(e))

    def who(p) -> str:
        if p.is_service:
            raise HTTPException(403, "project memory belongs to people and the agents acting for them")
        return p.urn

    def person(p) -> str:
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "a person does this themselves, not an agent")
        return p.urn

    @router.get("")
    async def projects(p=Depends(current_principal)):
        return await call(pm.list, who(p))

    @router.post("", status_code=201)
    async def create(body: ProjectIn, p=Depends(current_principal)):
        return await call(pm.create, person(p), body.name, body.description, body.team)

    @router.get("/{name}")
    async def one(name: str, p=Depends(current_principal)):
        return await call(pm.get, who(p), name)

    @router.put("/{name}")
    async def change(name: str, body: ProjectPatch, p=Depends(current_principal)):
        return await call(pm.update, person(p), name, body.description, body.team)

    @router.delete("/{name}", status_code=204)
    async def remove(name: str, p=Depends(current_principal)):
        await call(pm.delete, person(p), name)
        return Response(status_code=204)

    @router.get("/{name}/memories")
    async def memories(name: str, kind: Optional[str] = None, p=Depends(current_principal)):
        return await call(pm.memories, who(p), name, kind)

    @router.post("/{name}/memories", status_code=201)
    async def remember(name: str, body: MemoryIn, p=Depends(current_principal)):
        return await call(pm.remember, who(p), name, body.content, body.kind, body.shared,
                          by_agent=p.via in ("agent", "extension"))

    @router.post("/{name}/memories/recall")
    async def recall(name: str, body: RecallIn, p=Depends(current_principal)):
        return await call(pm.recall, who(p), name, body.query, body.k, body.kind)

    @router.post("/{name}/memories/{memory_id}/promote")
    async def promote(name: str, memory_id: str, p=Depends(current_principal)):
        return await call(pm.promote, person(p), name, memory_id)

    @router.delete("/{name}/memories/{memory_id}", status_code=204)
    async def forget(name: str, memory_id: str, p=Depends(current_principal)):
        await call(pm.forget, who(p), name, memory_id)
        return Response(status_code=204)

    @router.get("/{name}/rules")
    async def rules(name: str, p=Depends(current_principal)):
        return await call(pm.rules, who(p), name)

    return router


# ---- the command line: cairn agents memory ------------------------------------------------------------------------
PROJECT_FILE = ".cairn/project.json"


def add_parser(ag_sub) -> None:
    m = ag_sub.add_parser("memory", help="project memory and learned rules for the coding agents in a repository")
    m.add_argument("--project", help=f"the project's name (default: the one in {PROJECT_FILE})")
    m.add_argument("--dir", default=".", help="the repository (default: here)")
    m.add_argument("--profile", help="the sign-in to use (default: as other cairn commands)")
    m.add_argument("--json", action="store_true", help="print Cairn's answer as JSON")
    ms = m.add_subparsers(dest="memory_cmd", required=True)
    x = ms.add_parser("init", help=f"make the project in Cairn (or join it) and note its name in {PROJECT_FILE}")
    x.add_argument("--description", default="")
    x.add_argument("--team", help="a group (urn:li:corpGroup:…) whose members share the project")
    x = ms.add_parser("list", help="the project's notes and rules you can see")
    x.add_argument("--kind", choices=KINDS)
    x = ms.add_parser("add", help="remember a note, or a rule with --rule; --shared for the whole project")
    x.add_argument("text")
    x.add_argument("--rule", action="store_true")
    x.add_argument("--shared", action="store_true")
    x = ms.add_parser("recall", help="what the project's memory holds about something")
    x.add_argument("query")
    x.add_argument("-k", type=int, default=5)
    ms.add_parser("promote", help="share your proposed rule with the project").add_argument("id")
    ms.add_parser("forget", help="delete a memory").add_argument("id")
    x = ms.add_parser("sync", help="write the project's shared rules into AGENTS.md (Cairn's rules section)")
    x.add_argument("--check", action="store_true", help="change nothing; exit 1 if AGENTS.md is out of date")


def cli(a) -> None:
    import json
    import os
    import sys

    from cairn.agentkit import library
    from cairn_cli.errors import CliError
    from cairn_cli.session import Session

    def fail(why: str, code: int = 1):
        print(f"cairn agents memory: {why}", file=sys.stderr)
        raise SystemExit(code)

    def read(rel: str) -> Optional[str]:
        try:
            path = library.safe_path(a.dir, rel)
        except library.LibraryError as e:
            fail(str(e))
        return open(path, encoding="utf-8").read() if os.path.isfile(path) else None

    def write(rel: str, content: str) -> None:
        path = library.safe_path(a.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.cairn-{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

    def project() -> str:
        if a.project:
            return a.project
        try:
            name = json.loads(read(PROJECT_FILE) or "{}").get("project")
        except (ValueError, AttributeError):
            name = None
        if not isinstance(name, str) or not NAME.match(name):
            fail(f"no project: give --project, or run `cairn agents memory init` here ({PROJECT_FILE})")
        return name

    def show(doc: Any) -> None:
        if a.json:
            print(json.dumps(doc, indent=2))
            return
        for m in doc if isinstance(doc, list) else [doc]:
            if "content" in m:
                score = f"  ({m['score']:.2f})" if "score" in m else ""
                print(f"{m['id']}  {m['kind']:<4}  {m['status']:<8}  {' '.join(m['content'].split())}{score}")
            else:
                print(f"{m['name']}  owner {m['owner']}" + (f"  team {m['team']}" if m.get("team") else ""))

    try:
        s = Session.open(a.profile, quiet=True)
        try:
            cmd = a.memory_cmd
            if cmd == "init":
                name = a.project or re.sub(r"[^a-z0-9-]+", "-", os.path.basename(os.path.abspath(a.dir)).lower()).strip("-")
                if not NAME.match(name or ""):
                    fail("the folder's name does not make a project name: give --project")
                r = s.request("POST", "/v1/agent-projects", ok=(409,),
                              json={"name": name, "description": a.description, "team": a.team})
                doc = s.call("GET", f"/v1/agent-projects/{name}")     # 404 here: the name is someone else's
                wanted = json.dumps({"project": name}, indent=2) + "\n"
                if read(PROJECT_FILE) != wanted:
                    write(PROJECT_FILE, wanted)
                print(("Made" if r.status_code == 201 else "Using") + f" project {name}; noted in {PROJECT_FILE}",
                      file=sys.stderr)
                show(doc)
                return
            base = f"/v1/agent-projects/{project()}"
            if cmd == "list":
                show(s.call("GET", f"{base}/memories", params={"kind": a.kind} if a.kind else None))
            elif cmd == "add":
                show(s.call("POST", f"{base}/memories", {"content": a.text, "kind": "rule" if a.rule else "note",
                                                         "shared": a.shared}))
            elif cmd == "recall":
                show(s.call("POST", f"{base}/memories/recall", {"query": a.query, "k": a.k}))
            elif cmd == "promote":
                show(s.call("POST", f"{base}/memories/{a.id}/promote"))
            elif cmd == "forget":
                s.call("DELETE", f"{base}/memories/{a.id}")
            elif cmd == "sync":
                name = project()
                try:
                    merged = merge_rules(read("AGENTS.md"), rules_block(name, s.call("GET", f"{base}/rules")))
                except ValueError as e:
                    fail(str(e))
                if read("AGENTS.md") == merged:
                    print("AGENTS.md is up to date", file=sys.stderr)
                elif a.check:
                    fail("AGENTS.md is out of date: run `cairn agents memory sync`")
                else:
                    write("AGENTS.md", merged)
                    print("wrote AGENTS.md", file=sys.stderr)
        finally:
            s.close()
    except CliError as e:
        fail(str(e), getattr(e, "code", 1) or 1)
    except library.LibraryError as e:
        fail(str(e))
