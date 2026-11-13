"""Git folders: a person's saved queries and notebooks kept in a git repository, reviewed like code.

A folder links a path on a branch of a repository to its owner's SQL workspace (``POST /v1/git-folders``):
``name``, ``repo`` (a repository configured for Cairn's git integration, which holds its token) or ``url``
(one the operator allows, ``CAIRN_GIT_FOLDER_URLS``), ``branch`` (``main``), ``path`` (``""``: the root) and
optionally ``every_minutes`` for a scheduled pull. Cairn works on a clone of its own with the ``git`` command,
so any git host works.

* **Pull** (``POST /v1/git-folders/<id>/pull``): each ``.sql`` file becomes one of the owner's saved queries,
  and each ``.ipynb`` notebook is published to the catalog (with the tables its ``%%sql`` cells read). A file's
  header comments give the query's settings::

      -- connection: lakehouse
      -- description: Revenue by region, for the weekly review
      -- shared: true
      -- chart: {"type": "bar", "x": "region", "y": ["total"]}
      SELECT region, sum(amount) AS total FROM marts.orders GROUP BY region

  A file that changed in git updates its query. A deleted file deletes it. When the query was also changed in
  Cairn since the last pull, the file is a **conflict**: it is reported and left alone, unless ``force``.
* **Push** (``POST /v1/git-folders/<id>/push``): the folder's queries, as they are in Cairn (and any others of
  the owner's named in ``add``), are committed by the owner to a new branch ``cairn/<user>/<time>``, never to
  the folder's branch. For a configured GitHub, GitLab or Bitbucket repository, a pull request is opened
  from it. Merging the pull request and pulling again brings the folder back in step.
* **Notebooks in Jupyter** (``GET /v1/git-folders/<id>/notebooks``): a folder's notebooks appear in its owner's
  JupyterHub server under ``git/<folder>/`` (``cairn_lakehouse.jupyter`` syncs them when the server starts and
  on demand). Saving one there (the save hook sends it to Cairn) keeps it with the folder; the next push
  commits it, with its outputs cleared (query results stay out of git, and diffs stay reviewable). A pull
  that brings a new version of a notebook also changed in Jupyter since reports a conflict, unless ``force``.
* Folders are their owner's: others neither see nor use them. Each pull and push is audited.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, and_, delete, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.workspace import WorkspaceError, saved_queries

logger = logging.getLogger(__name__)

git_folders = Table(
    "cairn_git_folders", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(128), nullable=False),
    Column("repo", String(64), nullable=True),        # a configured repository (its token)
    Column("url", String(1024), nullable=True),        # or a URL the operator allows
    Column("branch", String(256), nullable=False),
    Column("path", String(512), nullable=False),
    Column("every_minutes", Integer, nullable=True),
    Column("head", String(64), nullable=True),         # the commit last pulled
    Column("synced_ms", BigInteger, nullable=True),
    Column("error", String(2000), nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)
git_folder_files = Table(
    "cairn_git_folder_files", metadata,
    Column("folder_id", String(32), primary_key=True),
    Column("file", String(1024), primary_key=True),    # relative to the folder's path
    Column("kind", String(16), nullable=False),        # query | notebook
    Column("ref", String(512), nullable=False),        # the saved query's id, or the notebook's urn
    Column("hash", String(64), nullable=False),        # of the file as last pulled ("": only pushed so far)
    Column("pushed", String(64), nullable=True),       # of the file as last pushed
)

git_folder_notebooks = Table(
    "cairn_git_folder_notebooks", metadata,
    Column("folder_id", String(32), primary_key=True),
    Column("file", String(1024), primary_key=True),    # relative to the folder's path
    Column("content", Text, nullable=False),           # as saved in Jupyter, outputs cleared
    Column("hash", String(64), nullable=False),
    Column("saved_ms", BigInteger, nullable=False),
)

NOTEBOOK_MAX = 5_000_000
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
SUBPATH = re.compile(r"^(?:[A-Za-z0-9._ -]+/)*[A-Za-z0-9._ -]*$")
HEADER = re.compile(r"^--\s*(connection|description|shared|chart|name)\s*:\s*(.*)$")
AUTH_USER = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}
HOSTS = {"github": "https://github.com/{repo}.git", "gitlab": "https://gitlab.com/{repo}.git",
         "bitbucket": "https://bitbucket.org/{repo}.git"}


def _now() -> int:
    return int(time.time() * 1000)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_sql(text: str, stem: str) -> dict:
    """A query file: its header comments, then the SQL."""
    out: dict[str, Any] = {"name": stem, "description": "", "shared": False, "chart": None, "connection": ""}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = HEADER.match(lines[i].strip())
        if not m:
            break
        key, value = m.group(1), m.group(2).strip()
        if key == "shared":
            out["shared"] = value.lower() in ("true", "yes", "1")
        elif key == "chart":
            try:
                out["chart"] = json.loads(value)
            except ValueError:
                raise WorkspaceError(f"{stem}: chart is JSON")
        else:
            out[key] = value
        i += 1
    out["sql"] = "\n".join(lines[i:]).strip()
    return out


def render_sql(q: dict) -> str:
    head = [f"-- connection: {q['connection']}"]
    if q.get("description"):
        head.append(f"-- description: {' '.join(str(q['description']).split())}")
    if q.get("shared"):
        head.append("-- shared: true")
    chart = q.get("chart") or {}
    if chart and chart != {"type": "table", "x": "", "y": []}:
        head.append(f"-- chart: {json.dumps(chart, sort_keys=True)}")
    return "\n".join(head) + "\n" + q["sql"].strip() + "\n"


def _same_query(spec: dict, q) -> bool:
    """Does a file say what the saved query is (name aside: a file may be renamed)?"""
    get = (lambda k: q[k]) if isinstance(q, dict) else (lambda k: getattr(q, k))
    chart = get("chart")
    chart = json.loads(chart or "{}") if isinstance(chart, str) else (chart or {})
    want = spec.get("chart") or {"type": "table", "x": "", "y": []}
    want = {"type": want.get("type", "table"), "x": str(want.get("x") or ""), "y": list(want.get("y") or [])}
    return (spec["sql"].strip() == str(get("sql")).strip() and spec["connection"] == get("connection")
            and " ".join(str(spec.get("description") or "").split()) == " ".join(str(get("description") or "").split())
            and bool(spec.get("shared")) == bool(get("shared")) and want == chart)


def clean_notebook(nb: dict) -> str:
    """A notebook as Cairn commits it: outputs and execution counts cleared, in nbformat's own layout."""
    nb = json.loads(json.dumps(nb))
    for c in nb.get("cells") or []:
        if isinstance(c, dict) and c.get("cell_type") == "code":
            c["outputs"] = []
            c["execution_count"] = None
    return json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _canonical(text: str) -> str:
    """A notebook file compared by what it says, not its outputs or layout."""
    try:
        return clean_notebook(json.loads(text))
    except (ValueError, TypeError, AttributeError):
        return text


def repo_remote(repo) -> tuple[str, Optional[str]]:
    """A configured repository's clone URL, and the HTTP header that signs in to it (None: public)."""
    url = repo.api_url and repo.provider == "gitlab" and repo.api_url.rstrip("/").removesuffix("/api/v4")
    url = f"{url}/{repo.repo}.git" if url else HOSTS[repo.provider].format(repo=repo.repo)
    if not repo.token:
        return url, None
    cred = base64.b64encode(f"{AUTH_USER[repo.provider]}:{repo.token}".encode()).decode()
    return url, f"Authorization: Basic {cred}"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return s[:120] or "query"


class GitFolders:
    def __init__(self, engine, workspace, git=None, notebooks=None, audit=None, workdir: Optional[str] = None,
                 allowed_urls: Optional[list[str]] = None):
        """``git``: Cairn's GitService (its configured repositories and their tokens); ``notebooks``: the
        NotebookCatalog notebooks are published to; ``allowed_urls``: URL prefixes folders may name."""
        self.engine, self.ws, self.git, self.notebooks, self.audit = engine, workspace, git, notebooks, audit
        self.workdir = workdir or os.path.join(os.environ.get("CAIRN_DATA_DIR", "/tmp/cairn"), "git-folders")
        self.allowed = [u for u in (allowed_urls or []) if u]
        self._locks: dict[str, threading.Lock] = {}

    # ---- definitions --------------------------------------------------------------------
    def _repo(self, rid: str):
        if self.git is None:
            raise WorkspaceError("no git repositories are configured", 404)
        try:
            return self.git.repo(rid)
        except Exception:
            raise WorkspaceError(f"no repository {rid!r}", 404)

    def create(self, actor: str, body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        rid, url = body.get("repo"), body.get("url")
        branch, path = str(body.get("branch") or "main"), str(body.get("path") or "").strip("/")
        every = body.get("every_minutes")
        if not name or len(name) > 128:
            raise WorkspaceError("a folder needs a name")
        if bool(rid) == bool(url):
            raise WorkspaceError("give repo (a configured repository) or url, not both")
        if rid:
            self._repo(str(rid))
        else:
            url = str(url)
            if not any(url.startswith(p) for p in self.allowed):
                raise WorkspaceError("that URL is not one folders may use (CAIRN_GIT_FOLDER_URLS)", 403)
        if not BRANCH.match(branch) or ".." in branch or branch.startswith(("-", "/")):
            raise WorkspaceError("branch: a branch name")
        if not SUBPATH.match(path) or ".." in path.split("/"):
            raise WorkspaceError("path: a folder in the repository")
        if every is not None and (isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 1440):
            raise WorkspaceError("every_minutes is 5-1440")
        if any(_slug(f["name"]).lower() == _slug(name).lower() for f in self.list(actor)):   # one directory each
            raise WorkspaceError(f"you already have a folder named {name!r} (its notebooks go in git/{_slug(name)}/)", 409)
        row = {"id": uuid.uuid4().hex, "owner": actor, "name": name, "repo": rid or None, "url": url if not rid else None,
               "branch": branch, "path": path, "every_minutes": every, "created_ms": _now()}
        with self.engine.begin() as conn:
            conn.execute(git_folders.insert().values(**row))
            if self.audit is not None:
                self.audit.record("sql", "git_folder.created", actor, None, row, conn=conn)
        return self.get(actor, row["id"])

    def get(self, actor: str, fid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(git_folders).where(and_(git_folders.c.id == fid, git_folders.c.owner == actor))).first()
        if r is None:
            raise WorkspaceError("no such git folder of yours", 404)
        return dict(r._mapping)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(git_folders).where(git_folders.c.owner == actor)
                                                           .order_by(git_folders.c.name))]

    def files(self, actor: str, fid: str) -> list[dict]:
        """The folder's files as the editor's file tree shows them (L163): each saved query it holds, by file, with
        ``state``: ``synced`` (as last pulled or pushed), ``modified`` (saved in Cairn since and not pushed as it is),
        ``proposed`` (pushed as it is, on a branch for review) or ``missing`` (the saved query was deleted). Notebooks
        are listed too (``kind: notebook``). Only the folder's owner sees them, as for the folder itself."""
        f = self.get(actor, fid)
        with self.engine.connect() as conn:
            rows = list(conn.execute(select(git_folder_files).where(git_folder_files.c.folder_id == fid)
                                     .order_by(git_folder_files.c.file)))
        mine = {q["id"]: q for q in self.ws.saved(actor) if q["owner"] == actor}
        out = []
        for r in rows:
            item = {"file": r.file, "kind": r.kind, "ref": r.ref, "name": r.file.rsplit("/", 1)[-1]}
            if r.kind == "query":
                q = mine.get(r.ref)
                if q is None:
                    item["state"] = "missing"
                else:
                    item["name"] = q["name"]
                    now = _hash(render_sql(q))
                    if now == r.hash:                     # git's branch has it (also once a pushed branch is merged)
                        item["state"] = "synced"
                    elif r.pushed == now:                 # pushed as it is, waiting in a branch for review
                        item["state"] = "proposed"
                    elif (f.get("synced_ms") or 0) >= q["updated_ms"]:
                        item["state"] = "synced"
                    else:
                        item["state"] = "modified"
            else:
                item["state"] = "synced"
            out.append(item)
        return out

    @staticmethod
    def jupyter_dir(f: dict) -> str:
        """Where a folder's notebooks are in its owner's Jupyter server."""
        return f"git/{_slug(f['name'])}"

    def delete(self, actor: str, fid: str) -> None:
        """Forgets the folder; its saved queries stay, as ordinary ones."""
        self.get(actor, fid)
        with self.engine.begin() as conn:
            conn.execute(delete(git_folder_notebooks).where(git_folder_notebooks.c.folder_id == fid))
            conn.execute(delete(git_folder_files).where(git_folder_files.c.folder_id == fid))
            conn.execute(delete(git_folders).where(git_folders.c.id == fid))
            if self.audit is not None:
                self.audit.record("sql", "git_folder.deleted", actor, None, {"id": fid}, conn=conn)
        shutil.rmtree(os.path.join(self.workdir, fid), ignore_errors=True)

    # ---- git ------------------------------------------------------------------------------
    def _remote(self, f: dict) -> tuple[str, list[str], Any]:
        """The URL, the git options that sign in (never written to disk), and the configured repo."""
        if f["repo"]:
            repo = self._repo(f["repo"])
            url, header = repo_remote(repo)
            return url, (["-c", f"http.extraHeader={header}"] if header else []), repo
        return f["url"], [], None

    def _git(self, cwd: Optional[str], *args: str, opts: Optional[list[str]] = None, timeout: int = 300) -> str:
        allowed = "https:ssh" + (":file" if any(p.startswith("file://") for p in self.allowed) else "")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.workdir, "GIT_TERMINAL_PROMPT": "0",
               "GIT_ALLOW_PROTOCOL": allowed, "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C"}
        r = subprocess.run(["git", *(opts or []), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise WorkspaceError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-500:]}", 502)
        return r.stdout

    def _checkout(self, f: dict) -> tuple[str, list[str], Any]:
        """The folder's clone, fetched and at the tip of its branch."""
        url, opts, repo = self._remote(f)
        d = os.path.join(self.workdir, f["id"])
        os.makedirs(self.workdir, exist_ok=True)
        if not os.path.isdir(os.path.join(d, ".git")):
            shutil.rmtree(d, ignore_errors=True)
            self._git(None, "clone", "--quiet", "--no-tags", "--branch", f["branch"], "--", url, d, opts=opts)
        else:
            self._git(d, "fetch", "--quiet", "--no-tags", "origin", f["branch"], opts=opts)
        self._git(d, "checkout", "--quiet", "--force", "-B", f["branch"], f"origin/{f['branch']}")
        self._git(d, "clean", "-fdq")
        return d, opts, repo

    def _files(self, d: str, path: str) -> dict[str, str]:
        base = os.path.join(d, path) if path else d
        out: dict[str, str] = {}
        if not os.path.isdir(base):
            return out
        for root, dirs, names in os.walk(base):
            dirs[:] = [x for x in dirs if x != ".git"]
            for n in names:
                if n.endswith((".sql", ".ipynb")):
                    full = os.path.join(root, n)
                    if os.path.islink(full) or os.path.getsize(full) > 5_000_000:
                        continue
                    out[os.path.relpath(full, base)] = open(full, encoding="utf-8", errors="replace").read()
        return out

    # ---- pull -------------------------------------------------------------------------------
    def pull(self, actor: str, fid: str, force: bool = False) -> dict:
        f = self.get(actor, fid)
        with self._locks.setdefault(fid, threading.Lock()):
            try:
                d, _, _ = self._checkout(f)
            except WorkspaceError as e:
                with self.engine.begin() as conn:
                    conn.execute(update(git_folders).where(git_folders.c.id == fid).values(error=str(e)[:2000]))
                raise
            head = self._git(d, "rev-parse", "HEAD").strip()
            files = self._files(d, f["path"])
            with self.engine.connect() as conn:
                known = {r.file: dict(r._mapping) for r in conn.execute(
                    select(git_folder_files).where(git_folder_files.c.folder_id == fid))}
                queries = {r.id: r for r in conn.execute(select(saved_queries).where(saved_queries.c.owner == actor))}
            since = f["synced_ms"] or 0
            with self.engine.connect() as conn:
                saves = {r.file: r for r in conn.execute(
                    select(git_folder_notebooks).where(git_folder_notebooks.c.folder_id == fid))}
            out = {"head": head, "created": [], "updated": [], "deleted": [], "notebooks": [], "conflicts": [], "errors": []}
            for file, text in sorted(files.items()):
                h, k = _hash(text), known.get(file)
                if k and k["hash"] == h and (k["kind"] == "notebook" or k["ref"] in queries):
                    continue                                  # unchanged in git
                try:
                    if file.endswith(".ipynb"):
                        nb_json = json.loads(text)
                        saved = saves.get(file)
                        if saved is not None and saved.content != _canonical(text):
                            if saved.saved_ms > since and not force:
                                out["conflicts"].append({"file": file, "query": None,
                                                         "why": "changed in git and in Jupyter since the last pull"})
                                continue
                        if saved is not None:                 # git's version is now the one to keep
                            with self.engine.begin() as conn:
                                conn.execute(delete(git_folder_notebooks).where(and_(
                                    git_folder_notebooks.c.folder_id == fid, git_folder_notebooks.c.file == file)))
                        ref = file
                        if self.notebooks is not None:
                            ref = self.notebooks.publish(actor, f"{self.jupyter_dir(f)}/{file}", nb_json).get("urn", file)
                        self._remember(fid, file, "notebook", ref, h)
                        out["notebooks"].append(file)
                        continue
                    spec = parse_sql(text, os.path.splitext(os.path.basename(file))[0])
                    q = queries.get(k["ref"]) if k else None
                    if q is not None and _same_query(spec, q):
                        self._remember(fid, file, "query", q.id, h)    # git now says what Cairn does (a merged push)
                        continue
                    if q is not None and q.updated_ms > since and not force:
                        out["conflicts"].append({"file": file, "query": q.id,
                                                 "why": "changed in git and in Cairn since the last pull"})
                        continue
                    saved = self.ws.save(actor, spec, q.id if q is not None else None)
                    self._remember(fid, file, "query", saved["id"], h)
                    out["updated" if q is not None else "created"].append(file)
                except (WorkspaceError, ValueError) as e:
                    out["errors"].append({"file": file, "error": str(e)})
            for file, k in sorted(known.items()):
                if file in files or not k["hash"]:
                    continue                                  # still there, or pushed and not merged yet
                q = queries.get(k["ref"]) if k["kind"] == "query" else None
                if q is not None and q.updated_ms > since and not force:
                    out["conflicts"].append({"file": file, "query": q.id, "why": "deleted in git, changed in Cairn"})
                    continue
                saved = saves.get(file) if k["kind"] == "notebook" else None
                if saved is not None and saved.saved_ms > since and not force:
                    out["conflicts"].append({"file": file, "query": None, "why": "deleted in git, changed in Jupyter"})
                    continue
                if saved is not None:
                    with self.engine.begin() as conn:
                        conn.execute(delete(git_folder_notebooks).where(and_(
                            git_folder_notebooks.c.folder_id == fid, git_folder_notebooks.c.file == file)))
                if q is not None:
                    self.ws.delete(actor, q.id)
                with self.engine.begin() as conn:
                    conn.execute(delete(git_folder_files).where(and_(git_folder_files.c.folder_id == fid,
                                                                     git_folder_files.c.file == file)))
                out["deleted"].append(file)
            with self.engine.begin() as conn:
                conn.execute(update(git_folders).where(git_folders.c.id == fid).values(
                    head=head, synced_ms=_now(), error=None))
                if self.audit is not None:
                    self.audit.record("sql", "git_folder.pulled", actor, None,
                                      {"id": fid, **{k: v for k, v in out.items() if k != "errors"},
                                       "errors": len(out["errors"])}, conn=conn)
        return out

    def _remember(self, fid: str, file: str, kind: str, ref: str, h: Optional[str] = None, pushed: Optional[str] = None) -> None:
        """Records a file as pulled (``h``) or pushed (``pushed``), keeping the other."""
        where = and_(git_folder_files.c.folder_id == fid, git_folder_files.c.file == file)
        with self.engine.begin() as conn:
            old = conn.execute(select(git_folder_files).where(where)).first()
            conn.execute(delete(git_folder_files).where(where))
            conn.execute(git_folder_files.insert().values(
                folder_id=fid, file=file, kind=kind, ref=ref,
                hash=h if h is not None else (old.hash if old is not None else ""),
                pushed=pushed if pushed is not None else (old.pushed if old is not None else None)))

    # ---- notebooks in Jupyter -----------------------------------------------------------------
    def notebook_saved(self, actor: str, path: str, notebook: dict) -> Optional[dict]:
        """A notebook saved in the person's Jupyter server: kept with the folder whose directory holds it
        (``git/<folder>/<file>``), for the next push. Other paths are not a folder's: None."""
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "git" or not path.endswith(".ipynb"):
            return None
        folder = next((f for f in self.list(actor) if _slug(f["name"]) == parts[1]), None)
        if folder is None:
            return None
        file = "/".join(parts[2:])
        if not SUBPATH.match(file) or any(x in ("", ".", "..") for x in parts[2:]):
            raise WorkspaceError("path: a notebook in the folder (plain names, no ..)")
        content = clean_notebook(notebook)
        if len(content) > NOTEBOOK_MAX:
            raise WorkspaceError("the notebook is over 5 MB without its outputs", 413)
        where = and_(git_folder_notebooks.c.folder_id == folder["id"], git_folder_notebooks.c.file == file)
        with self.engine.begin() as conn:
            conn.execute(delete(git_folder_notebooks).where(where))
            conn.execute(git_folder_notebooks.insert().values(folder_id=folder["id"], file=file, content=content,
                                                              hash=_hash(content), saved_ms=_now()))
        return {"folder": folder["id"], "file": file}

    def jupyter_notebooks(self, actor: str, fid: str) -> list[dict]:
        """What the owner's Jupyter server should hold for the folder: each notebook as last saved in Jupyter,
        or else as git has it (from the last pull or push)."""
        f = self.get(actor, fid)
        d = os.path.join(self.workdir, fid)
        with self._locks.setdefault(fid, threading.Lock()):
            from_git = {k: v for k, v in self._files(d, f["path"]).items() if k.endswith(".ipynb")} \
                if os.path.isdir(os.path.join(d, ".git")) else {}
        with self.engine.connect() as conn:
            saves = {r.file: r.content for r in conn.execute(
                select(git_folder_notebooks).where(git_folder_notebooks.c.folder_id == fid))}
        out = []
        for file in sorted(set(from_git) | set(saves)):
            text = saves.get(file, from_git.get(file))
            try:
                nb = json.loads(text)
            except ValueError:
                continue                                      # not a notebook git can open either
            out.append({"file": file, "path": f"{self.jupyter_dir(f)}/{file}", "hash": _hash(_canonical(text)),
                        "notebook": nb, "saved_in_jupyter": file in saves})
        return out

    # ---- push -------------------------------------------------------------------------------
    def push(self, actor: str, fid: str, message: str = "", add: Optional[list[str]] = None) -> dict:
        f = self.get(actor, fid)
        user = actor.removeprefix("urn:li:corpuser:")
        with self._locks.setdefault(fid, threading.Lock()):
            d, opts, repo = self._checkout(f)
            base = os.path.join(d, f["path"]) if f["path"] else d
            with self.engine.connect() as conn:
                rows = list(conn.execute(select(git_folder_files).where(and_(
                    git_folder_files.c.folder_id == fid, git_folder_files.c.kind == "query"))))
            known = {r.ref: r.file for r in rows}
            proposed = {r.ref: r.pushed for r in rows}
            mine = {q["id"]: q for q in self.ws.saved(actor) if q["owner"] == actor}
            for qid in add or []:
                if qid not in mine:
                    raise WorkspaceError(f"no saved query of yours {qid!r}", 404)
            written: dict[str, tuple[str, str, str]] = {}      # file -> (kind, query id or notebook urn, text)
            taken = set(known.values())
            for qid in list(known) + [q for q in (add or []) if q not in known]:
                q = mine.get(qid)
                if q is None:
                    continue
                file = known.get(qid)
                if file is None:
                    stem, n = _slug(q["name"]), 1
                    file = f"{stem}.sql"
                    while file in taken or os.path.exists(os.path.join(base, file)):
                        n += 1
                        file = f"{stem}-{n}.sql"
                    taken.add(file)
                text = render_sql(q)
                full = os.path.join(base, file)
                if proposed.get(qid) == _hash(text):
                    continue                                  # already proposed as it is
                if os.path.exists(full) and _same_query(parse_sql(open(full, encoding="utf-8").read(), q["name"]), q):
                    continue                                  # git already says this
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(text)
                written[file] = ("query", qid, text)
            # Notebooks saved in Jupyter under the folder, outputs already cleared.
            from cairn.identity.notebooks import notebook_urn

            with self.engine.connect() as conn:
                saves = list(conn.execute(select(git_folder_notebooks).where(git_folder_notebooks.c.folder_id == fid)))
                nb_pushed = {r.file: r.pushed for r in conn.execute(select(git_folder_files).where(and_(
                    git_folder_files.c.folder_id == fid, git_folder_files.c.kind == "notebook")))}
            for r in saves:
                full = os.path.join(base, r.file)
                if nb_pushed.get(r.file) == r.hash:
                    continue                                  # already proposed as it is
                if os.path.exists(full) and _canonical(open(full, encoding="utf-8").read()) == r.content:
                    continue                                  # git already says this
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(r.content)
                written[r.file] = ("notebook", notebook_urn(user, f"{self.jupyter_dir(f)}/{r.file}"), r.content)
            self._git(d, "add", "--all", "--", f["path"] or ".")
            if not self._git(d, "status", "--porcelain").strip():
                return {"changed": 0, "branch": None, "pull_request": None}
            branch = f"cairn/{_slug(user)}/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            self._git(d, "checkout", "--quiet", "-b", branch)
            ident = ["-c", f"user.name={user}", "-c", f"user.email={_slug(user)}@users.noreply.cairn"]
            self._git(d, "commit", "--quiet", "-m", message or f"Saved queries from Cairn ({f['name']})", opts=ident)
            self._git(d, "push", "--quiet", "origin", f"{branch}:refs/heads/{branch}", opts=opts)
            sha = self._git(d, "rev-parse", "HEAD").strip()
            self._git(d, "checkout", "--quiet", "--force", f["branch"])
            pr = None
            if repo is not None and self.git is not None:
                pr = self.git.provider(repo).create_pull(branch, f["branch"], message or f"Saved queries from Cairn ({f['name']})",
                                                         f"Pushed from Cairn's SQL workspace by {user}.")
            for file, (kind, ref, text) in written.items():
                self._remember(fid, file, kind, ref, pushed=_hash(text))
            out = {"changed": len(written), "branch": branch, "commit": sha, "pull_request": pr}
            if self.audit is not None:
                self.audit.record("sql", "git_folder.pushed", actor, None, {"id": fid, **out})
        return out

    def commit_files(self, actor: Optional[str], fid: str, files: dict[str, Optional[str]], message: str,
                     new_branch: Optional[str] = None, pr_body: str = "") -> dict:
        """Commits ``files`` (a path under the folder's path -> its text, or None to remove it) to the folder's
        repository. With ``new_branch``, on a new branch made from the folder's branch (``cairn/<new_branch>``), with
        a pull request for a configured repository: a reviewable change (an infrastructure-as-code change request).
        Without it, straight onto the folder's branch: what a deployment does (Airflow's DAG folder kept by
        git-sync). ``actor``: the person committing, who must own the folder (None: Cairn itself, for a folder the
        operator configured)."""
        if actor is None:
            with self.engine.connect() as conn:
                r = conn.execute(select(git_folders).where(git_folders.c.id == fid)).first()
            if r is None:
                raise WorkspaceError(f"no git folder {fid!r}", 404)
            f = dict(r._mapping)
        else:
            f = self.get(actor, fid)
        for rel in files:
            if not SUBPATH.match(rel) or not rel or rel.endswith("/") or ".." in rel.split("/") or rel.startswith("/"):
                raise WorkspaceError(f"{rel!r} is not a file path in the folder")
        user = (actor or "urn:li:corpuser:cairn").removeprefix("urn:li:corpuser:")
        with self._locks.setdefault(fid, threading.Lock()):
            d, opts, repo = self._checkout(f)
            base = os.path.join(d, f["path"]) if f["path"] else d
            for rel, text in files.items():
                full = os.path.join(base, rel)
                if text is None:
                    if os.path.exists(full):
                        os.remove(full)
                    continue
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(text)
            self._git(d, "add", "--all", "--", f["path"] or ".")
            if not self._git(d, "status", "--porcelain").strip():
                return {"changed": 0, "branch": None, "commit": None, "pull_request": None}
            branch = f["branch"]
            if new_branch:
                branch = f"cairn/{_slug(new_branch)}"
                if not BRANCH.match(branch):
                    raise WorkspaceError("not a branch name")
                self._git(d, "checkout", "--quiet", "-B", branch)
            ident = ["-c", f"user.name={user}", "-c", f"user.email={_slug(user)}@users.noreply.cairn"]
            self._git(d, "commit", "--quiet", "-m", message, opts=ident)
            self._git(d, "push", "--quiet", *(["--force"] if new_branch else []), "origin",
                      f"{branch}:refs/heads/{branch}", opts=opts)
            sha = self._git(d, "rev-parse", "HEAD").strip()
            if new_branch:
                self._git(d, "checkout", "--quiet", "--force", f["branch"])
            pr = None
            if new_branch and repo is not None and self.git is not None:
                pr = self.git.provider(repo).create_pull(branch, f["branch"], message.splitlines()[0][:200], pr_body)
            out = {"changed": len(files), "branch": branch, "commit": sha, "pull_request": pr, "folder": f["name"]}
            if self.audit is not None:
                self.audit.record("sql", "git_folder.committed", actor or "urn:li:corpuser:cairn", None,
                                  {"id": fid, "files": sorted(files), **{k: out[k] for k in ("branch", "commit")}})
        return out

    def run_due(self) -> int:
        n = 0
        now = _now()
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(git_folders).where(git_folders.c.every_minutes.is_not(None)))]
        for f in due:
            if f["synced_ms"] and now - f["synced_ms"] < f["every_minutes"] * 60_000:
                continue
            try:
                self.pull(f["owner"], f["id"])
                n += 1
            except Exception as e:  # noqa: BLE001 - recorded on the folder; the others go on
                logger.warning("git folder %s: %s", f["id"], e)
        return n


def build_git_folders_router(gf: GitFolders, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/git-folders", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "git folders are a person's own")
        return p.urn

    async def call(fn, *a, **kw):
        try:
            return await run_in_threadpool(fn, *a, **kw)
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(gf.list, person(p))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(gf.create, person(p), body)

    @router.delete("/{fid}", status_code=204)
    async def remove(fid: str, p=Depends(current_principal)):
        await call(gf.delete, person(p), fid)

    @router.post("/{fid}/pull")
    async def pull(fid: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await call(gf.pull, person(p), fid, bool((body or {}).get("force")))

    @router.get("/{fid}/files")
    async def files(fid: str, p=Depends(current_principal)):
        return await call(gf.files, person(p), fid)

    @router.get("/{fid}/notebooks")
    async def notebooks(fid: str, p=Depends(current_principal)):
        return await call(gf.jupyter_notebooks, person(p), fid)

    @router.post("/{fid}/push")
    async def push(fid: str, body: dict = Body(default={}), p=Depends(current_principal)):
        body = body or {}
        add = body.get("add") or []
        if not isinstance(add, list):
            raise HTTPException(422, "add: saved query ids")
        return await call(gf.push, person(p), fid, str(body.get("message") or ""), [str(x) for x in add])

    return router
