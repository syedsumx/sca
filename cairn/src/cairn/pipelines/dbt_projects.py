"""dbt developed and run in Cairn (L172): a project's files, its environments, and compile, preview and build.

``cairn dbt`` (``cairn.pipelines.dbt_runner``) runs a project from a pipeline. This is the project in the product: what
an editor needs behind it.

* **Projects** (``/v1/dbt/projects``): a name and its files (``dbt_project.yml``, models, tests, seeds, macros,
  YAML), kept in Cairn and read and written one by one (``.../files/<path>``). No ``profiles.yml``: a project never
  holds a connection or a credential.
* **Environments** (``.../environments``): where a project runs: a Trino connection of Cairn's, the schema it
  builds into, threads. ``dev``, ``ci``, ``prod`` are just names.
* **Commands** (``POST .../commands`` with ``{"command", "environment", "select"}``): ``parse``, ``compile``,
  ``run``, ``test``, ``seed`` or ``build``, by dbt itself, on Trino **as the person who asks**: Cairn mints them a
  short Trino token and writes it to a private profile that is deleted with the run. Their policies, masks and row
  filters apply to every query, and what dbt may create is what they may create. The answer has each node's result,
  compiled SQL for ``compile``, and the end of dbt's log; a run that built something is recorded like any dbt run
  (docs, lineage, tests, the environment's state: ``cairn.pipelines.dbt``).
* **State-aware** (``"state_aware": true``): only what changed since the environment's last successful run, and
  what depends on it (``state:modified+``, deferring the rest to that run). Nothing recorded yet: everything runs.
* **Preview** (``POST .../preview`` with ``{"model"}`` or ``{"sql"}``): dbt compiles it, and the compiled query
  runs through Cairn's governed query path as the person, with a row limit. Nothing is built.

* **The pull-request build** (``POST .../ci`` with ``{"environment": "<the base>", "label": "pr-42", "files":
  {path: content or null}}``): the project with the pull request's files laid over it (they are not saved), built
  by dbt in a schema of its own (``<schema>_ci_<label>_<random>``), state-aware against the base environment's last
  successful run and deferring to it, so only what the pull request changes is built and tested. The schema is
  dropped when the build ends, pass or fail. The answer has each node's result and a Markdown report for the pull
  request. It is not recorded as the environment's run: it is not the environment.
* **Lint** (``POST .../lint``): dbt compiles the project and Cairn checks every model's compiled SQL (it must parse
  as Trino SQL; ``SELECT *`` in a model's result, comma joins, ``ORDER BY`` without ``LIMIT``, unnamed expressions)
  and its documentation (a description, at least one test).

dbt runs on the API's host (``CAIRN_DBT_BIN``, else ``dbt`` on its PATH with dbt-trino); without one, commands answer
503 and the files and environments still work. A project is its owner's (and operators'); making one needs
``MANAGE_INGESTION``. Commands are run by people, never by a service or agent credential.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, Text, and_, delete, insert, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
ENV = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
EXTENSIONS = (".sql", ".yml", ".yaml", ".md", ".csv", ".py", ".txt")
COMMANDS = ("parse", "compile", "run", "test", "seed", "build")
SELECT = re.compile(r"^[A-Za-z0-9_.:+@*,/ -]{1,500}$")
MAX_FILE, MAX_FILES, TIMEOUT = 1_000_000, 2000, 1800
PRIVILEGE = "MANAGE_INGESTION"

dbt_projects = Table(
    "cairn_dbt_projects", metadata,
    Column("name", String(64), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("description", String(2000), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
dbt_project_files = Table(
    "cairn_dbt_project_files", metadata,
    Column("project", String(64), nullable=False),
    Column("path", String(512), nullable=False),
    Column("content", Text, nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("project", "path"),
)
dbt_environments = Table(
    "cairn_dbt_environments", metadata,
    Column("project", String(64), nullable=False),
    Column("name", String(32), nullable=False),
    Column("connection", String(128), nullable=False),
    Column("schema", String(64), nullable=False),
    Column("threads", Integer, nullable=False),
    PrimaryKeyConstraint("project", "name"),
)


class DbtProjectError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def file_path(path: str) -> str:
    parts = str(path or "").split("/")
    if not path or len(path) > 512 or any(not SEGMENT.match(p) or p in (".", "..") for p in parts):
        raise DbtProjectError("a file's path is segments of letters, digits, '.', '_' and '-', separated by '/'")
    if not path.lower().endswith(EXTENSIONS):
        raise DbtProjectError(f"a project's files end in {', '.join(EXTENSIONS)}")
    if parts[-1].lower() in ("profiles.yml", "profiles.yaml"):
        raise DbtProjectError("a project holds no profiles.yml: Cairn connects it, as the person who runs it")
    if parts[0] in ("target", "logs", "dbt_packages"):
        raise DbtProjectError(f"{parts[0]}/ is dbt's own output, not part of the project")
    return path


def dbt_command() -> Optional[list[str]]:
    explicit = os.environ.get("CAIRN_DBT_BIN")
    if explicit:
        return [explicit] if os.path.isfile(explicit) else None
    if shutil.which("dbt"):
        return [shutil.which("dbt")]
    try:
        import dbt.cli.main  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-c", "from dbt.cli.main import cli; cli()"]


class DbtProjects:
    def __init__(self, engine, allowed: Callable[[str, str, Optional[str]], bool], connection: Callable[[str], Optional[dict]],
                 mint: Callable[[str], dict], query: Callable[[str, str, str, int], dict],
                 record: Optional[Callable[[dict, str], dict]] = None, state: Optional[Callable[[str, str], Optional[dict]]] = None,
                 audit=None, run: Optional[Callable[[str, str, str], list]] = None):
        """``connection(name)``: a Cairn connection's definition (a ``trino://`` URL); ``mint(actor)``: a short Trino
        token for that person (``{"token", "user"}``); ``query(actor, sql, connection, limit)``: the governed query
        path; ``record(body, actor)`` and ``state(project, environment)``: dbt runs kept by Cairn."""
        self.engine, self.allowed, self.connection, self.mint, self.query = engine, allowed, connection, mint, query
        self.record, self.state, self.audit = record, state, audit
        self.run = run                  # run(actor, connection, sql): a statement on Trino as the person (the CI schema's drop)

    # ---- projects --------------------------------------------------------------------------------------
    def _operator(self, actor: str) -> bool:
        return self.allowed(actor, "MANAGE_SYSTEM_OPERATIONS", None)

    def _project(self, name: str, actor: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(dbt_projects).where(dbt_projects.c.name == name)).first()
        if r is None or not (r.owner == actor or self._operator(actor)):
            raise DbtProjectError("no such dbt project", 404)
        return dict(r._mapping)

    def _envs(self, name: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [{"project": r.project, "name": r.name, "connection": r.connection, "schema": r.schema, "threads": r.threads}
                    for r in conn.execute(select(dbt_environments).where(dbt_environments.c.project == name)
                                          .order_by(dbt_environments.c.name))]

    def _public(self, row: dict) -> dict:
        with self.engine.connect() as conn:
            files = [{"path": r.path, "updated_by": r.updated_by, "updated_ms": r.updated_ms}
                     for r in conn.execute(select(dbt_project_files.c.path, dbt_project_files.c.updated_by,
                                                  dbt_project_files.c.updated_ms)
                                           .where(dbt_project_files.c.project == row["name"]).order_by(dbt_project_files.c.path))]
        return {"name": row["name"], "owner": row["owner"], "description": row["description"], "files": files,
                "environments": self._envs(row["name"]), "created_ms": row["created_ms"]}

    def create(self, actor: str, body: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        if not self.allowed(actor, PRIVILEGE, None):
            raise DbtProjectError(f"making dbt projects needs {PRIVILEGE}", 403)
        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise DbtProjectError("name: lowercase letters, digits and _ (dbt's own rule for a project's name)")
        files = body.get("files") or {}
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise DbtProjectError(f"files: {{path: content}}, at most {MAX_FILES}")
        if "dbt_project.yml" not in files:      # a project that parses from the start
            files = {"dbt_project.yml": f'name: {name}\nversion: "1.0"\nprofile: {name}\nmodel-paths: ["models"]\n', **files}
        checked = {file_path(p): self._content(c) for p, c in files.items()}
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(dbt_projects).values(name=name, owner=actor, description=str(body.get("description") or "")[:2000],
                                                         created_ms=_now()))
                for p, c in checked.items():
                    conn.execute(insert(dbt_project_files).values(project=name, path=p, content=c, updated_by=actor,
                                                                  updated_ms=_now()))
        except IntegrityError:
            raise DbtProjectError(f"a dbt project {name} exists", 409)
        self._audit("dbt_project.created", actor, name, {"files": len(checked)})
        return self.get(actor, name)

    def update(self, actor: str, name: str, body: dict) -> dict:
        self._project(name, actor)
        if "description" in body:
            with self.engine.begin() as conn:
                conn.execute(dbt_projects.update().where(dbt_projects.c.name == name)
                             .values(description=str(body["description"] or "")[:2000]))
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        return self._public(self._project(name, actor))

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(dbt_projects).order_by(dbt_projects.c.name))]
        everyone = self._operator(actor)
        return [{k: v for k, v in self._public(r).items() if k != "files"} for r in rows if everyone or r["owner"] == actor]

    def delete(self, actor: str, name: str) -> None:
        self._project(name, actor)
        with self.engine.begin() as conn:
            for t in (dbt_project_files, dbt_environments):
                conn.execute(delete(t).where(t.c.project == name))
            conn.execute(delete(dbt_projects).where(dbt_projects.c.name == name))
        self._audit("dbt_project.deleted", actor, name, {})

    # ---- files -----------------------------------------------------------------------------------------
    @staticmethod
    def _content(content: Any) -> str:
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise DbtProjectError(f"a file is text, at most {MAX_FILE} bytes")
        return content

    def read(self, actor: str, name: str, path: str) -> dict:
        self._project(name, actor)
        with self.engine.connect() as conn:
            r = conn.execute(select(dbt_project_files).where(and_(dbt_project_files.c.project == name,
                                                                  dbt_project_files.c.path == path))).first()
        if r is None:
            raise DbtProjectError("no such file", 404)
        return {"path": r.path, "content": r.content, "updated_by": r.updated_by, "updated_ms": r.updated_ms}

    def write(self, actor: str, name: str, path: str, content: Any) -> dict:
        self._project(name, actor)
        path, content = file_path(path), self._content(content)
        with self.engine.begin() as conn:
            n = conn.execute(select(dbt_project_files.c.path).where(dbt_project_files.c.project == name)).all()
            if len(n) >= MAX_FILES and path not in {r[0] for r in n}:
                raise DbtProjectError(f"a project holds at most {MAX_FILES} files")
            conn.execute(delete(dbt_project_files).where(and_(dbt_project_files.c.project == name,
                                                              dbt_project_files.c.path == path)))
            conn.execute(insert(dbt_project_files).values(project=name, path=path, content=content, updated_by=actor,
                                                          updated_ms=_now()))
        return {"path": path, "size": len(content.encode())}

    def remove(self, actor: str, name: str, path: str) -> None:
        self._project(name, actor)
        if path == "dbt_project.yml":
            raise DbtProjectError("a project keeps its dbt_project.yml")
        with self.engine.begin() as conn:
            if not conn.execute(delete(dbt_project_files).where(and_(dbt_project_files.c.project == name,
                                                                    dbt_project_files.c.path == path))).rowcount:
                raise DbtProjectError("no such file", 404)

    # ---- environments ----------------------------------------------------------------------------------
    def set_environment(self, actor: str, name: str, env: str, body: dict) -> dict:
        self._project(name, actor)
        connection, schema, threads = str(body.get("connection") or ""), str(body.get("schema") or ""), body.get("threads", 4)
        if not ENV.match(env or ""):
            raise DbtProjectError("an environment's name: lowercase letters, digits, - and _")
        if not SCHEMA.match(schema):
            raise DbtProjectError("schema: the schema this environment builds into (lowercase letters, digits and _)")
        if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 32:
            raise DbtProjectError("threads: between 1 and 32")
        self._trino(connection)
        with self.engine.begin() as conn:
            conn.execute(delete(dbt_environments).where(and_(dbt_environments.c.project == name, dbt_environments.c.name == env)))
            conn.execute(insert(dbt_environments).values(project=name, name=env, connection=connection, schema=schema,
                                                         threads=threads))
        self._audit("dbt_project.environment_set", actor, name, {"environment": env, "connection": connection, "schema": schema})
        return next(e for e in self._envs(name) if e["name"] == env)

    def remove_environment(self, actor: str, name: str, env: str) -> None:
        self._project(name, actor)
        with self.engine.begin() as conn:
            if not conn.execute(delete(dbt_environments).where(and_(dbt_environments.c.project == name,
                                                                   dbt_environments.c.name == env))).rowcount:
                raise DbtProjectError("no such environment", 404)

    def _trino(self, connection: str) -> dict:
        d = self.connection(connection) if connection else None
        url = urlsplit(str((d or {}).get("url") or ""))
        if d is None or url.scheme != "trino" or not url.hostname:
            raise DbtProjectError("connection: one of Cairn's Trino connections (trino://...)")
        opts = {k: v[-1] for k, v in parse_qs(url.query).items()}
        return {"host": url.hostname, "port": url.port or 8443, "catalog": url.path.strip("/").split("/")[0] or "lakehouse",
                "scheme": opts.get("protocol", "https"), "verify": opts.get("verify")}

    # ---- dbt -------------------------------------------------------------------------------------------
    def _materialize(self, name: str, root: Path) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(select(dbt_project_files.c.path, dbt_project_files.c.content)
                                .where(dbt_project_files.c.project == name)).all()
        for path, content in rows:
            f = root / file_path(path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)

    def _names(self, root: Path, name: str) -> tuple[str, str]:
        """dbt's own name for the project (its runs and state are kept under it) and its profile's name."""
        import yaml

        try:
            doc = yaml.safe_load((root / "dbt_project.yml").read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise DbtProjectError(f"dbt_project.yml does not read: {str(e)[:200]}")
        if not isinstance(doc, dict):
            raise DbtProjectError("dbt_project.yml is not a mapping")
        return str(doc.get("name") or name), str(doc.get("profile") or name)

    def _dbt(self, actor: str, name: str, env: str, args: list[str], state_aware: bool = False,
             overlay: Optional[dict] = None, schema: Optional[str] = None) -> dict:
        """Runs dbt on the project's files as ``actor``: ``{"code", "log", "manifest", "run_results"}``."""
        import yaml

        cmd = dbt_command()
        if cmd is None:
            raise DbtProjectError("dbt is not installed beside Cairn's API (CAIRN_DBT_BIN, or dbt with dbt-trino on its PATH)", 503)
        e = next((x for x in self._envs(name) if x["name"] == env), None)
        if e is None:
            raise DbtProjectError(f"the project has no environment {env}", 404)
        trino = self._trino(e["connection"])
        try:
            tt = self.mint(actor)
        except Exception as ex:  # noqa: BLE001 - no token, no run
            raise DbtProjectError(f"Cairn could not sign you in to Trino ({type(ex).__name__})", 503)
        work = Path(tempfile.mkdtemp(prefix="cairn-dbt-"))
        os.chmod(work, 0o700)
        try:
            project, profiles, target = work / "project", work / "profiles", work / "target"
            project.mkdir()
            profiles.mkdir(mode=0o700)
            self._materialize(name, project)
            for path, content in (overlay or {}).items():        # a pull request's files, over the project's
                f = project / path
                if content is None:
                    f.unlink(missing_ok=True)
                else:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(content)
            out = {"type": "trino", "method": "jwt", "jwt_token": tt["token"], "user": tt.get("user") or actor.rsplit(":", 1)[-1],
                   "host": trino["host"], "port": trino["port"], "http_scheme": trino["scheme"],
                   "cert": trino["verify"] or True, "database": trino["catalog"], "schema": schema or e["schema"],
                   "threads": e["threads"], "client_tags": ["cairn", "dbt-project"], "session_properties": {},
                   "prepared_statements_enabled": True}
            pf = profiles / "profiles.yml"
            pf.touch(mode=0o600)
            dbt_name, profile = self._names(project, name)
            pf.write_text(yaml.safe_dump({profile: {"target": env, "outputs": {env: out}}}))
            extra: list[str] = []
            if state_aware and self.state is not None:
                last = self.state(dbt_name, env)
                manifest = (last or {}).get("manifest")
                if isinstance(manifest, dict):
                    (work / "state").mkdir()
                    (work / "state" / "manifest.json").write_text(json.dumps(manifest))
                    extra = ["--select", "state:modified+", "--defer", "--state", str(work / "state")]
            env_vars = {k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "AWS_", "ANTHROPIC", "OPENAI"))}
            env_vars.update({"DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "DBT_PROFILES_DIR": str(profiles)})
            if trino["verify"]:        # requests lets these override the CA the profile names: the connection's CA only
                for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                    env_vars.pop(k, None)
            started = _now()
            try:
                p = subprocess.run([*cmd, *args, *extra, "--project-dir", str(project), "--profiles-dir", str(profiles),
                                    "--target-path", str(target), "--log-path", str(work / "logs"), "--no-use-colors"],
                                   env=env_vars, capture_output=True, text=True, timeout=TIMEOUT, cwd=str(project))
            except subprocess.TimeoutExpired:
                raise DbtProjectError(f"dbt did not finish in {TIMEOUT} seconds", 504)
            log = (p.stdout + p.stderr).replace(tt["token"], "***")

            def artifact(file: str) -> Optional[dict]:
                try:
                    return json.loads((target / file).read_text())
                except (OSError, ValueError):
                    return None
            return {"code": p.returncode, "log": log[-8000:], "manifest": artifact("manifest.json"),
                    "run_results": artifact("run_results.json"), "target": target, "started_ms": started,
                    "state_aware": bool(extra), "compiled": self._compiled(target, name) if args[0] == "compile" else None,
                    "catalog": trino["catalog"], "connection": e["connection"], "schema": schema or e["schema"]}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _compiled(target: Path, name: str) -> dict:
        out = {}
        base = target / "compiled"
        for f in sorted(base.rglob("*.sql")) if base.is_dir() else []:
            if f.is_file():                              # an inline query compiles into a folder named like a file
                out[str(f.relative_to(base)).split("/", 1)[-1]] = f.read_text()[:200_000]
        return out

    def command(self, actor: str, name: str, body: dict) -> dict:
        self._project(name, actor)
        command, env, select_ = str(body.get("command") or ""), str(body.get("environment") or ""), body.get("select")
        if command not in COMMANDS:
            raise DbtProjectError(f"command: one of {', '.join(COMMANDS)}")
        args = [command]
        state_aware = bool(body.get("state_aware")) and command in ("run", "build", "test")
        if select_:
            if state_aware:
                raise DbtProjectError("state_aware chooses what runs: give it or select, not both")
            if not isinstance(select_, str) or not SELECT.match(select_) or select_.lstrip().startswith("-"):
                raise DbtProjectError("select: dbt's node selection (names, tags, + and @ operators)")
            args += ["--select", *select_.split()]
        got = self._dbt(actor, name, env, args, state_aware)
        results = [{"unique_id": r.get("unique_id"), "status": r.get("status"), "seconds": round(float(r.get("execution_time") or 0), 3),
                    "message": (str(r.get("message"))[:500] if r.get("message") else None)}
                   for r in ((got["run_results"] or {}).get("results") or [])]
        out = {"project": name, "environment": env, "command": command, "ok": got["code"] == 0, "exit_code": got["code"],
               "state_aware": got["state_aware"], "results": results, "log": got["log"]}
        if got["compiled"] is not None:
            out["compiled"] = got["compiled"]
        if command in ("run", "build", "test", "seed") and got["manifest"] and got["run_results"] and self.record is not None:
            try:
                rec = self.record({"manifest": got["manifest"], "run_results": got["run_results"], "command": command,
                                   "environment": env, "started_ms": got["started_ms"]}, actor)
                out["recorded"] = {k: rec.get(k) for k in ("models", "lineage_edges", "tests") if k in rec}
            except Exception as e:  # noqa: BLE001 - the run happened; that it was not recorded is said
                logger.warning("dbt project %s: run not recorded: %s", name, e)
                out["recorded"] = None
                out["record_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        self._audit("dbt_project.command", actor, name, {"command": command, "environment": env, "ok": out["ok"],
                                                         "state_aware": out["state_aware"], "nodes": len(results)})
        return out

    def preview(self, actor: str, name: str, body: dict) -> dict:
        """A model's (or a query's) rows, without building anything: compiled by dbt, run by Cairn as the person."""
        self._project(name, actor)
        env, model, sql = str(body.get("environment") or ""), body.get("model"), body.get("sql")
        limit = body.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise DbtProjectError("limit: between 1 and 1000")
        if bool(model) == bool(sql):
            raise DbtProjectError("give model (a model's name) or sql (a query with {{ ref() }})")
        if model:
            if not isinstance(model, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$", model):
                raise DbtProjectError("model: a model's name")
            got = self._dbt(actor, name, env, ["compile", "--select", model])
            compiled = next((v for k, v in (got["compiled"] or {}).items() if k.rsplit("/", 1)[-1] == f"{model}.sql"), None)
        else:
            if not isinstance(sql, str) or not sql.strip() or len(sql) > 100_000:
                raise DbtProjectError("sql: a query")
            got = self._dbt(actor, name, env, ["compile", "--inline", sql, "--output", "json", "--quiet"])
            compiled = None
            m = re.search(r"\{.*\}", got["log"], re.S)           # dbt prints {"compiled": "..."} for an inline query
            try:
                compiled = json.loads(m.group(0)).get("compiled") if m else None
            except (ValueError, AttributeError):
                compiled = None
        if got["code"] != 0 or not compiled:
            raise DbtProjectError("dbt could not compile it: " + got["log"][-1500:], 422)
        e = next(x for x in self._envs(name) if x["name"] == env)
        try:
            out = self.query(actor, self._local(compiled, self._trino(e["connection"])["catalog"]), e["connection"], limit)
        except Exception as ex:  # noqa: BLE001 - the warehouse's or the policies' reason
            raise DbtProjectError(f"the compiled query did not run: {str(ex)[:500]}", getattr(ex, "status", 422))
        return {"compiled": compiled, "columns": out["columns"], "rows": out["rows"], "truncated": out.get("truncated", False),
                "masked_columns": out.get("masked_columns", [])}

    # ---- the pull-request build ------------------------------------------------------------------------
    def ci(self, actor: str, name: str, body: dict) -> dict:
        self._project(name, actor)
        env, label = str(body.get("environment") or ""), str(body.get("label") or "ci").lower()
        if not re.match(r"^[a-z0-9][a-z0-9_]{0,23}$", label):
            raise DbtProjectError("label: what the build is for, e.g. pr_42 (lowercase letters, digits and _, at most 24)")
        files = body.get("files") or {}
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise DbtProjectError("files: the pull request's files, {path: content, or null for a removed file}")
        overlay = {file_path(p): (None if c is None else self._content(c)) for p, c in files.items()}
        base = next((x for x in self._envs(name) if x["name"] == env), None)
        if base is None:
            raise DbtProjectError(f"the project has no environment {env}", 404)
        if self.run is None:
            raise DbtProjectError("this Cairn cannot drop the build's schema afterwards: the build is not started", 503)
        schema = f"{base['schema']}_ci_{label}_{os.urandom(3).hex()}"[:63]
        try:
            got = self._dbt(actor, name, env, ["build"], state_aware=True, overlay=overlay, schema=schema)
        finally:
            dropped = self._drop_schema(actor, base["connection"], schema)
        results = [{"unique_id": r.get("unique_id"), "status": r.get("status"), "seconds": round(float(r.get("execution_time") or 0), 3),
                    "message": (str(r.get("message"))[:500] if r.get("message") else None)}
                   for r in ((got["run_results"] or {}).get("results") or [])]
        ok = got["code"] == 0
        out = {"project": name, "base_environment": env, "label": label, "schema": schema, "ok": ok, "exit_code": got["code"],
               "compared_with_state": got["state_aware"], "results": results, "schema_dropped": dropped, "log": got["log"]}
        out["report"] = self._report(out)
        self._audit("dbt_project.ci", actor, name, {"environment": env, "label": label, "ok": ok, "nodes": len(results),
                                                    "files": len(overlay), "schema_dropped": dropped})
        return out

    def _drop_schema(self, actor: str, connection: str, schema: str) -> bool:
        """Drops the build's schema and what dbt made in it. True when it is gone (or was never made)."""
        catalog = self._trino(connection)["catalog"]
        try:
            self.run(actor, connection, f'DROP SCHEMA IF EXISTS "{catalog}"."{schema}" CASCADE')
            return True
        except Exception as e:  # noqa: BLE001 - said on the build, never hidden
            logger.warning("dbt CI schema %s.%s was not dropped: %s", catalog, schema, e)
            return False

    @staticmethod
    def _report(out: dict) -> str:
        mark = {"success": "✅", "pass": "✅", "skipped": "⏭️", "warn": "⚠️"}
        lines = [f"### dbt build for `{out['label']}`: {'passed' if out['ok'] else 'failed'}", "",
                 ("Built what this change modifies, and what depends on it, against the last successful "
                  f"`{out['base_environment']}` run." if out["compared_with_state"] else
                  f"No successful `{out['base_environment']}` run to compare with: everything was built."), ""]
        if out["results"]:
            lines += ["| | Node | Result | Seconds |", "|---|---|---|---|"]
            lines += [f"| {mark.get(r['status'], '❌')} | `{r['unique_id']}` | {r['status']}"
                      + (f": {' '.join(r['message'].split())[:160]}" if r["message"] and r["status"] not in ("success", "pass") else "")
                      + f" | {r['seconds']} |" for r in out["results"]]
        else:
            lines.append("Nothing in this change alters a model, seed, snapshot or test." if out["ok"] else
                         "dbt stopped before building anything:\n\n```\n" + out["log"][-1200:] + "\n```")
        if not out["schema_dropped"]:
            lines += ["", f"The build's schema `{out['schema']}` could not be dropped: drop it by hand."]
        return "\n".join(lines)

    # ---- lint ------------------------------------------------------------------------------------------
    def lint(self, actor: str, name: str, body: dict) -> dict:
        import sqlglot
        from sqlglot import exp

        self._project(name, actor)
        env = str(body.get("environment") or "")
        got = self._dbt(actor, name, env, ["compile"])
        if got["code"] != 0 or not got["manifest"]:
            raise DbtProjectError("dbt could not compile the project: " + got["log"][-1500:], 422)
        nodes = got["manifest"].get("nodes") or {}
        tested = {uid for n in nodes.values() if n.get("resource_type") in ("test", "unit_test")
                  for uid in (n.get("depends_on") or {}).get("nodes") or []}
        findings: list[dict] = []

        def add(node: dict, rule: str, severity: str, message: str) -> None:
            findings.append({"rule": rule, "severity": severity, "model": node.get("name"),
                             "file": node.get("original_file_path"), "message": message})
        models = 0
        for uid, n in sorted(nodes.items()):
            if n.get("resource_type") != "model" or n.get("package_name") != got["manifest"].get("metadata", {}).get("project_name"):
                continue
            models += 1
            if not str(n.get("description") or "").strip():
                add(n, "D001", "warning", "the model has no description")
            if uid not in tested and (n.get("config") or {}).get("materialized") != "ephemeral":
                add(n, "D002", "warning", "the model has no test")
            sql = n.get("compiled_code") or ""
            try:
                tree = sqlglot.parse_one(sql, read="trino")
            except sqlglot.errors.SqlglotError as e:
                add(n, "S001", "error", f"the compiled SQL does not parse as Trino SQL: {str(e).splitlines()[0][:200]}")
                continue
            final = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
            if final is not None and any(isinstance(x, exp.Star) for x in final.expressions):
                add(n, "S002", "warning", "SELECT * in the model's result: a new upstream column changes this model's shape")
            for sel in tree.find_all(exp.Select):
                frm = sel.args.get("from_") or sel.args.get("from")
                if any(not j.args.get("on") and not j.args.get("using") and not j.kind and not j.side and not j.method
                       for j in sel.args.get("joins") or []) and frm is not None:
                    add(n, "S003", "warning", "a comma join: write the join and its condition (JOIN ... ON)")
                    break
            if isinstance(tree, exp.Select) and tree.args.get("order") is not None and tree.args.get("limit") is None:
                add(n, "S004", "info", "ORDER BY without LIMIT in a model: the order is not kept by the table and costs a sort")
            if final is not None:
                unnamed = [x for x in final.expressions if not isinstance(x, (exp.Column, exp.Alias, exp.Star))]
                if unnamed:
                    add(n, "S005", "warning", f"{len(unnamed)} expression(s) in the result have no name (AS ...)")
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")}
        return {"project": name, "models": models, "findings": findings, "summary": counts, "ok": counts["error"] == 0}

    @staticmethod
    def _local(sql: str, catalog: str) -> str:
        """The query with the connection's own catalog left off its tables: dbt writes "catalog"."schema"."table",
        and Cairn's query path names the connection's tables schema.table (and checks each against the catalog)."""
        import sqlglot
        from sqlglot import exp

        try:
            tree = sqlglot.parse_one(sql, read="trino")
        except sqlglot.errors.SqlglotError:
            return sql                                   # said by the query path, which parses it again
        for t in tree.find_all(exp.Table):
            if t.catalog and t.catalog.lower() == catalog.lower():
                t.set("catalog", None)
        return tree.sql(dialect="trino")

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, f"urn:li:cairnDbtProject:{name}", details)


def build_dbt_projects_router(dp: DbtProjects, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/dbt/projects", tags=["dbt"])

    def person(p) -> str:
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "dbt projects are worked on by people: dbt runs as them")
        return p.urn

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except DbtProjectError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(dp.list, person(p))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(dp.create, person(p), body)

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(dp.get, person(p), name)

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(dp.update, person(p), name, body)

    @router.delete("/{name}", status_code=204)
    async def remove(name: str, p=Depends(current_principal)):
        await call(dp.delete, person(p), name)

    @router.get("/{name}/files/{path:path}")
    async def read(name: str, path: str, p=Depends(current_principal)):
        return await call(dp.read, person(p), name, path)

    @router.put("/{name}/files/{path:path}")
    async def write(name: str, path: str, content: str = Body(..., embed=True), p=Depends(current_principal)):
        return await call(dp.write, person(p), name, path, content)

    @router.delete("/{name}/files/{path:path}", status_code=204)
    async def remove_file(name: str, path: str, p=Depends(current_principal)):
        await call(dp.remove, person(p), name, path)

    @router.put("/{name}/environments/{env}")
    async def set_environment(name: str, env: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(dp.set_environment, person(p), name, env, body)

    @router.delete("/{name}/environments/{env}", status_code=204)
    async def remove_environment(name: str, env: str, p=Depends(current_principal)):
        await call(dp.remove_environment, person(p), name, env)

    @router.post("/{name}/commands")
    async def command(name: str, body: dict = Body(...), p=Depends(current_principal)):
        """Runs a dbt command on the project, on Trino as the caller."""
        return await call(dp.command, person(p), name, body)

    @router.post("/{name}/ci")
    async def ci(name: str, body: dict = Body(...), p=Depends(current_principal)):
        """A pull request's build: its files over the project, built in a schema of its own that is then dropped."""
        return await call(dp.ci, person(p), name, body)

    @router.post("/{name}/lint")
    async def lint(name: str, body: dict = Body(...), p=Depends(current_principal)):
        """Every model's compiled SQL and documentation, checked."""
        return await call(dp.lint, person(p), name, body)

    @router.post("/{name}/preview")
    async def preview(name: str, body: dict = Body(...), p=Depends(current_principal)):
        """A model's or a query's rows: compiled by dbt, run through the governed query path. Nothing is built."""
        return await call(dp.preview, person(p), name, body)

    return router
