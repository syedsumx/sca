"""Git connectors: GitHub, GitLab and Bitbucket repositories holding dbt projects.

Repositories are reviewed YAML files in ``config/git`` (or ``CAIRN_GIT``); ``${ENV}`` values are read
from the environment, so tokens stay out of the files::

    repos:
      - id: analytics
        provider: github              # github | gitlab | bitbucket
        repo: acme/analytics-dbt      # GitLab: the project path; Bitbucket: workspace/slug
        token: ${GITHUB_TOKEN}
        webhook_secret: ${GITHUB_WEBHOOK_SECRET}
        dbt_project_dir: ""           # where dbt_project.yml is
        platform: snowflake           # the warehouse the models build into
        database: analytics
        schema: core

**Impact on pull requests.** A webhook (``/integrations/git/{id}/webhook``) on a pull or merge request
reads which dbt models it changes, finds them in the catalog, and posts one comment (updated on every
push) with, per model: downstream assets (certified first), dashboards, owners to tell, its data
contract, and columns the change removes. A commit status ``cairn/impact`` fails when a removed column
is one a contract promises.

**Docs back to dbt.** ``writeback`` opens a pull request that writes the descriptions people edited in
Cairn (the table's and its columns') into the model's YAML, so dbt stays the source of truth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
MARKER = "<!-- cairn-impact -->"
STATUS = "cairn/impact"


class GitError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class GitRepo(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    provider: Literal["github", "gitlab", "bitbucket"]
    repo: str
    token: str = ""
    webhook_secret: str = ""
    api_url: Optional[str] = None
    base_branch: str = "main"
    dbt_project_dir: str = ""
    platform: str = "snowflake"
    database: str = ""
    schema_: str = Field(default="", alias="schema")
    env: str = "PROD"

    model_config = {"populate_by_name": True}

    def public(self) -> dict:
        return {"id": self.id, "provider": self.provider, "repo": self.repo, "base_branch": self.base_branch,
                "dbt_project_dir": self.dbt_project_dir, "platform": self.platform, "database": self.database,
                "schema": self.schema_, "token_set": bool(self.token), "webhook_secret_set": bool(self.webhook_secret)}


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    return v


def load_repos(path: Optional[str] = None) -> dict[str, GitRepo]:
    path = path or os.environ.get("CAIRN_GIT", "/etc/cairn/config/git")
    if not path or not os.path.exists(path):
        return {}
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")) and ".example." not in f)
    out: dict[str, GitRepo] = {}
    for f in files:
        with open(f) as fh:
            for raw in (yaml.safe_load(fh) or {}).get("repos") or []:
                r = GitRepo.model_validate({k: _expand(v) for k, v in raw.items()})
                out[r.id] = r
    return out


# ---- providers ----------------------------------------------------------------------------------
class Provider:
    """The few calls Cairn needs from a Git host."""

    def __init__(self, repo: GitRepo, http: Optional[httpx.Client] = None):
        self.r = repo
        self.http = http or httpx.Client(timeout=30)

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        resp = self.http.request(method, url, headers={**self.headers(), **kw.pop("headers", {})}, **kw)
        if resp.status_code >= 400:
            raise GitError(f"{self.r.provider} said {resp.status_code}: {resp.text[:300]}", 502)
        return resp

    def headers(self) -> dict:
        return {}

    # Each provider implements these.
    def changed_files(self, pr: str) -> list[dict]: ...
    def read(self, path: str, ref: str) -> Optional[str]: ...
    def tree(self, ref: str, prefix: str) -> list[str]: ...
    def upsert_comment(self, pr: str, body: str) -> None: ...
    def set_status(self, sha: str, ok: bool, description: str, url: str) -> None: ...
    def open_pr(self, branch: str, files: dict[str, str], title: str, body: str) -> str: ...


class GitHub(Provider):
    def base(self) -> str:
        return (self.r.api_url or "https://api.github.com").rstrip("/") + f"/repos/{self.r.repo}"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.r.token}", "Accept": "application/vnd.github+json"}

    def changed_files(self, pr: str) -> list[dict]:
        out, page = [], 1
        while page <= 10:
            items = self._call("GET", f"{self.base()}/pulls/{pr}/files", params={"per_page": 100, "page": page}).json()
            out += [{"path": i["filename"], "status": i["status"], "old_path": i.get("previous_filename")} for i in items]
            if len(items) < 100:
                break
            page += 1
        return out

    def pr_refs(self, pr: str) -> tuple[str, str]:
        p = self._call("GET", f"{self.base()}/pulls/{pr}").json()
        return p["base"]["sha"], p["head"]["sha"]

    def read(self, path: str, ref: str) -> Optional[str]:
        resp = self.http.get(f"{self.base()}/contents/{quote(path)}", params={"ref": ref},
                             headers={**self.headers(), "Accept": "application/vnd.github.raw+json"})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitError(f"github said {resp.status_code}: {resp.text[:300]}", 502)
        return resp.text

    def tree(self, ref: str, prefix: str) -> list[str]:
        items = self._call("GET", f"{self.base()}/git/trees/{ref}", params={"recursive": "1"}).json().get("tree") or []
        return [i["path"] for i in items if i.get("type") == "blob" and i["path"].startswith(prefix)]

    def upsert_comment(self, pr: str, body: str) -> None:
        comments = self._call("GET", f"{self.base()}/issues/{pr}/comments", params={"per_page": 100}).json()
        mine = next((c for c in comments if MARKER in (c.get("body") or "")), None)
        if mine:
            self._call("PATCH", f"{self.base()}/issues/comments/{mine['id']}", json={"body": body})
        else:
            self._call("POST", f"{self.base()}/issues/{pr}/comments", json={"body": body})

    def set_status(self, sha: str, ok: bool, description: str, url: str) -> None:
        self._call("POST", f"{self.base()}/statuses/{sha}", json={
            "state": "success" if ok else "failure", "context": STATUS, "description": description[:140],
            **({"target_url": url} if url else {})})

    def open_pr(self, branch: str, files: dict[str, str], title: str, body: str) -> str:
        base_sha = self._call("GET", f"{self.base()}/git/ref/heads/{self.r.base_branch}").json()["object"]["sha"]
        self._call("POST", f"{self.base()}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        for path, content in files.items():
            meta = self._call("GET", f"{self.base()}/contents/{quote(path)}", params={"ref": branch}).json()
            self._call("PUT", f"{self.base()}/contents/{quote(path)}", json={
                "message": title, "branch": branch, "sha": meta["sha"],
                "content": base64.b64encode(content.encode()).decode()})
        return self._call("POST", f"{self.base()}/pulls", json={"title": title, "head": branch,
                                                                "base": self.r.base_branch, "body": body}).json()["html_url"]


class GitLab(Provider):
    def base(self) -> str:
        return (self.r.api_url or "https://gitlab.com/api/v4").rstrip("/") + f"/projects/{quote(self.r.repo, safe='')}"

    def headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.r.token}

    def changed_files(self, pr: str) -> list[dict]:
        changes = self._call("GET", f"{self.base()}/merge_requests/{pr}/changes").json().get("changes") or []
        return [{"path": c["new_path"], "old_path": c.get("old_path"),
                 "status": "removed" if c.get("deleted_file") else "added" if c.get("new_file") else "modified"}
                for c in changes]

    def pr_refs(self, pr: str) -> tuple[str, str]:
        mr = self._call("GET", f"{self.base()}/merge_requests/{pr}").json()
        refs = mr.get("diff_refs") or {}
        return refs.get("base_sha") or mr["target_branch"], refs.get("head_sha") or mr["sha"]

    def read(self, path: str, ref: str) -> Optional[str]:
        resp = self.http.get(f"{self.base()}/repository/files/{quote(path, safe='')}/raw", params={"ref": ref},
                             headers=self.headers())
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitError(f"gitlab said {resp.status_code}: {resp.text[:300]}", 502)
        return resp.text

    def tree(self, ref: str, prefix: str) -> list[str]:
        out, page = [], 1
        while page <= 20:
            items = self._call("GET", f"{self.base()}/repository/tree", params={
                "ref": ref, "recursive": "true", "per_page": 100, "page": page, "path": prefix.rstrip("/") or None}).json()
            out += [i["path"] for i in items if i.get("type") == "blob"]
            if len(items) < 100:
                break
            page += 1
        return out

    def upsert_comment(self, pr: str, body: str) -> None:
        notes = self._call("GET", f"{self.base()}/merge_requests/{pr}/notes", params={"per_page": 100}).json()
        mine = next((n for n in notes if MARKER in (n.get("body") or "")), None)
        if mine:
            self._call("PUT", f"{self.base()}/merge_requests/{pr}/notes/{mine['id']}", json={"body": body})
        else:
            self._call("POST", f"{self.base()}/merge_requests/{pr}/notes", json={"body": body})

    def set_status(self, sha: str, ok: bool, description: str, url: str) -> None:
        self._call("POST", f"{self.base()}/statuses/{sha}", json={
            "state": "success" if ok else "failed", "name": STATUS, "description": description[:140],
            **({"target_url": url} if url else {})})

    def open_pr(self, branch: str, files: dict[str, str], title: str, body: str) -> str:
        self._call("POST", f"{self.base()}/repository/commits", json={
            "branch": branch, "start_branch": self.r.base_branch, "commit_message": title,
            "actions": [{"action": "update", "file_path": p, "content": c} for p, c in files.items()]})
        return self._call("POST", f"{self.base()}/merge_requests", json={
            "source_branch": branch, "target_branch": self.r.base_branch, "title": title,
            "description": body}).json()["web_url"]


class Bitbucket(Provider):
    def base(self) -> str:
        return (self.r.api_url or "https://api.bitbucket.org/2.0").rstrip("/") + f"/repositories/{self.r.repo}"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.r.token}"}

    def changed_files(self, pr: str) -> list[dict]:
        out, url = [], f"{self.base()}/pullrequests/{pr}/diffstat"
        while url and len(out) < 2000:
            page = self._call("GET", url).json()
            for v in page.get("values") or []:
                new, old = v.get("new") or {}, v.get("old") or {}
                out.append({"path": new.get("path") or old.get("path"), "old_path": old.get("path"),
                            "status": {"removed": "removed", "added": "added"}.get(v.get("status"), "modified")})
            url = page.get("next")
        return out

    def pr_refs(self, pr: str) -> tuple[str, str]:
        p = self._call("GET", f"{self.base()}/pullrequests/{pr}").json()
        return p["destination"]["commit"]["hash"], p["source"]["commit"]["hash"]

    def read(self, path: str, ref: str) -> Optional[str]:
        resp = self.http.get(f"{self.base()}/src/{ref}/{quote(path)}", headers=self.headers())
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitError(f"bitbucket said {resp.status_code}: {resp.text[:300]}", 502)
        return resp.text

    def tree(self, ref: str, prefix: str) -> list[str]:
        out, url = [], f"{self.base()}/src/{ref}/{quote(prefix)}?max_depth=20&pagelen=100"
        while url and len(out) < 5000:
            page = self._call("GET", url).json()
            out += [v["path"] for v in page.get("values") or [] if v.get("type") == "commit_file"]
            url = page.get("next")
        return out

    def upsert_comment(self, pr: str, body: str) -> None:
        comments = self._call("GET", f"{self.base()}/pullrequests/{pr}/comments", params={"pagelen": 100}).json()
        mine = next((c for c in comments.get("values") or [] if MARKER in ((c.get("content") or {}).get("raw") or "")),
                    None)
        if mine:
            self._call("PUT", f"{self.base()}/pullrequests/{pr}/comments/{mine['id']}", json={"content": {"raw": body}})
        else:
            self._call("POST", f"{self.base()}/pullrequests/{pr}/comments", json={"content": {"raw": body}})

    def set_status(self, sha: str, ok: bool, description: str, url: str) -> None:
        self._call("POST", f"{self.base()}/commit/{sha}/statuses/build", json={
            "state": "SUCCESSFUL" if ok else "FAILED", "key": "cairn-impact", "name": STATUS,
            "description": description[:140], "url": url or "https://cairn"})

    def open_pr(self, branch: str, files: dict[str, str], title: str, body: str) -> str:
        parent = self._call("GET", f"{self.base()}/refs/branches/{quote(self.r.base_branch)}").json()["target"]["hash"]
        self._call("POST", f"{self.base()}/src", data={"branch": branch, "message": title, "parents": parent, **files})
        return self._call("POST", f"{self.base()}/pullrequests", json={
            "title": title, "description": body, "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": self.r.base_branch}}}).json()["links"]["html"]["href"]


PROVIDERS = {"github": GitHub, "gitlab": GitLab, "bitbucket": Bitbucket}


# ---- webhooks -----------------------------------------------------------------------------------------
def pull_request_from(repo: GitRepo, headers: dict, body: bytes) -> Optional[str]:
    """The pull request a verified webhook is about, or None for an event Cairn ignores."""
    h = {k.lower(): v for k, v in headers.items()}
    if not repo.webhook_secret:
        raise GitError("the repository has no webhook_secret", 503)
    if repo.provider == "gitlab":
        if not hmac.compare_digest(h.get("x-gitlab-token", ""), repo.webhook_secret):
            raise GitError("bad webhook token", 401)
    else:
        sig = h.get("x-hub-signature-256" if repo.provider == "github" else "x-hub-signature", "")
        expected = "sha256=" + hmac.new(repo.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise GitError("bad webhook signature", 401)
    payload = json.loads(body or b"{}")
    if repo.provider == "github":
        if h.get("x-github-event") != "pull_request" or payload.get("action") not in ("opened", "synchronize",
                                                                                       "reopened", "ready_for_review"):
            return None
        return str(payload["pull_request"]["number"])
    if repo.provider == "gitlab":
        attrs = payload.get("object_attributes") or {}
        if payload.get("object_kind") != "merge_request" or attrs.get("action") not in ("open", "update", "reopen"):
            return None
        return str(attrs["iid"])
    if h.get("x-event-key") not in ("pullrequest:created", "pullrequest:updated"):
        return None
    return str(payload["pullrequest"]["id"])


# ---- dbt ----------------------------------------------------------------------------------------------
def _models_in_yaml(text: Optional[str]) -> dict[str, list[str]]:
    """``{model: [columns]}`` declared in a dbt properties file."""
    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    out = {}
    for m in doc.get("models") or [] if isinstance(doc, dict) else []:
        if isinstance(m, dict) and m.get("name"):
            out[str(m["name"])] = [str(c["name"]) for c in m.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    return out


def changed_models(provider: Provider, repo: GitRepo, files: list[dict], base: str, head: str) -> dict[str, dict]:
    """``{model: {"change": ..., "removed_columns": [...]}}`` from a pull request's files."""
    root = repo.dbt_project_dir.strip("/")
    root = f"{root}/" if root else ""
    out: dict[str, dict] = {}
    for f in files:
        path = f["path"] or ""
        if not path.startswith(root) or not path[len(root):].startswith(("models/", "snapshots/", "seeds/")):
            continue
        name, ext = os.path.splitext(os.path.basename(path))
        if ext == ".sql":                                 # the model's SQL outranks a properties-only change
            entry = out.setdefault(name, {"removed_columns": []})
            entry.update(change="removed" if f["status"] == "removed" else "added" if f["status"] == "added"
                         else "changed", file=path)
        elif ext in (".yml", ".yaml"):
            before = _models_in_yaml(provider.read(f.get("old_path") or path, base) if f["status"] != "added" else "")
            after = _models_in_yaml(provider.read(path, head) if f["status"] != "removed" else "")
            for model in set(before) | set(after):
                gone = [c for c in before.get(model, []) if c not in after.get(model, [])]
                if before.get(model) == after.get(model) and model in before and model in after:
                    continue
                entry = out.setdefault(model, {"change": "properties", "removed_columns": [], "file": path})
                entry["removed_columns"] = sorted(set(entry["removed_columns"]) | set(gone))
    return out


def model_datasets(repo: GitRepo, model: str) -> list[str]:
    name = ".".join(p for p in (repo.database, repo.schema_, model) if p)
    if repo.platform in ("snowflake", "bigquery"):
        name = name.lower()
    return [f"urn:li:dataset:(urn:li:dataPlatform:dbt,{name},{repo.env})",
            f"urn:li:dataset:(urn:li:dataPlatform:{repo.platform},{name},{repo.env})"]


def set_descriptions(text: str, model: str, table: Optional[str], columns: dict[str, str]) -> tuple[str, bool]:
    """The properties file with the model's (and its columns') descriptions set. Edits only those lines,
    so comments and formatting elsewhere survive."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^(\s*)-\s+name:\s*['\"]?{re.escape(model)}['\"]?\s*$", ln)),
                 None)
    if start is None:
        raise GitError(f"model {model} is not in this file", 404)
    indent = len(re.match(r"^(\s*)", lines[start]).group(1))
    body = indent + 2
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and
                len(lines[i]) - len(lines[i].lstrip()) <= indent), len(lines))
    def put(block_start: int, block_end: int, key_indent: int, value: str) -> int:
        """Set ``description`` in a block; returns how many lines the block grew by."""
        quoted = json.dumps(value, ensure_ascii=False)
        new = f"{' ' * key_indent}description: {quoted}\n"
        for i in range(block_start + 1, block_end):
            m = re.match(rf"^\s{{{key_indent}}}description:\s*(.*)$", lines[i].rstrip("\n"))
            if m:
                j = i + 1                                  # a block scalar's continuation lines go too
                if m.group(1).strip() in ("|", ">", "|-", ">-", "|+", ">+"):
                    while j < block_end and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > key_indent):
                        j += 1
                lines[i:j] = [new]
                return 1 - (j - i)
        lines.insert(block_start + 1, new)
        return 1

    if table:
        end += put(start, end, body, table)
    for col, desc in columns.items():
        col_start = next((i for i in range(start + 1, end) if re.match(
            rf"^(\s*)-\s+name:\s*['\"]?{re.escape(col)}['\"]?\s*$", lines[i])), None)
        if col_start is None:
            continue
        ci = len(re.match(r"^(\s*)", lines[col_start]).group(1))
        col_end = next((i for i in range(col_start + 1, end) if lines[i].strip() and
                        len(lines[i]) - len(lines[i].lstrip()) <= ci), end)
        end += put(col_start, col_end, ci + 2, desc)
    out = "".join(lines)
    return out, out != text


# ---- the service ----------------------------------------------------------------------------------------
class GitService:
    def __init__(self, repos: dict[str, GitRepo], graph, catalog=None, contracts=None, attributes=None, audit=None,
                 public_url: str = "", http: Optional[httpx.Client] = None):
        self.repos = repos
        self.graph = graph
        self.catalog = catalog
        self.contracts = contracts
        self.attributes = attributes
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self.http = http

    def repo(self, rid: str) -> GitRepo:
        r = self.repos.get(rid)
        if r is None:
            raise GitError(f"no repository {rid!r}", 404)
        return r

    def provider(self, repo: GitRepo) -> Provider:
        return PROVIDERS[repo.provider](repo, self.http)

    def _dataset(self, repo: GitRepo, model: str) -> Optional[str]:
        for urn in model_datasets(repo, model):
            if self.graph.get_aspect(urn, "schemaMetadata") or self.graph.get_aspect(urn, "datasetProperties"):
                return urn
        return None

    def _owners(self, urn: str) -> list[str]:
        own = self.graph.get_aspect(urn, "ownership") or {}
        return [o["owner"] for o in own.get("owners") or [] if o.get("owner")]

    def impact(self, rid: str, pr: str) -> dict:
        repo = self.repo(rid)
        prov = self.provider(repo)
        base, head = prov.pr_refs(pr)
        models = changed_models(prov, repo, prov.changed_files(pr), base, head)
        rows, broken = [], []
        for model, change in sorted(models.items()):
            urn = self._dataset(repo, model)
            row: dict[str, Any] = {"model": model, **change, "dataset": urn, "downstream": [], "certified": 0,
                                   "dashboards": 0, "owners": [], "contract": None, "breaks": []}
            if urn and self.catalog is not None:
                try:
                    down = self.catalog.lineage(urn, "DOWNSTREAM", 3, 200)
                except Exception:  # noqa: BLE001 - lineage is informative
                    down = []
                for d in down:
                    cert = (self.attributes.resource(d["urn"]).get("certification") if self.attributes else None)
                    row["downstream"].append({"urn": d["urn"], "name": d.get("name") or d["urn"],
                                              "certified": cert == "CERTIFIED", "type": d.get("type")})
                row["downstream"].sort(key=lambda d: (not d["certified"], d["name"]))
                row["certified"] = sum(1 for d in row["downstream"] if d["certified"])
                row["dashboards"] = sum(1 for d in row["downstream"] if "dashboard" in (d["urn"].split(":")[2]).lower())
                people = self._owners(urn)
                for d in row["downstream"][:30]:
                    people += [p for p in self._owners(d["urn"]) if p not in people]
                row["owners"] = people[:20]
            if urn and self.contracts is not None:
                c = self.contracts.get(urn)
                if c:
                    promised = {col["name"] for col in ((c["spec"].get("schema") or {}).get("columns") or [])}
                    row["contract"] = {"version": c["version"], "status": c["status"]}
                    row["breaks"] = sorted(promised & set(change["removed_columns"]))
                    if change["change"] == "removed":
                        row["breaks"] = sorted(promised) or ["the model itself"]
                    if row["breaks"]:
                        broken.append(model)
            rows.append(row)
        body = self.comment(rows)
        ok = not broken
        summary = (f"breaks the contract of {', '.join(broken)}" if broken else
                   f"{len(rows)} model(s) changed, {sum(len(r['downstream']) for r in rows)} downstream asset(s)")
        if rows or broken:
            prov.upsert_comment(pr, body)
        prov.set_status(head, ok, summary, f"{self.public_url}/cairn/search?q=dbt" if self.public_url else "")
        if self.audit is not None:
            self.audit.record("git", "git.impact_posted", "urn:li:corpuser:cairn-git", f"{rid}#{pr}",
                              {"models": len(rows), "broken": broken})
        return {"repo": rid, "pr": pr, "ok": ok, "summary": summary, "models": rows}

    def comment(self, rows: list[dict]) -> str:
        def link(urn, text):
            return (f"[{text}]({self.public_url}/cairn/asset/{quote(urn, safe='')})"
                        if self.public_url else text)
        out = [MARKER, "### Cairn: what this change affects", ""]
        if not rows:
            out.append("No dbt models change in this pull request.")
            return "\n".join(out)
        out += ["| Model | Change | Downstream | Certified | Dashboards | Contract |", "|---|---|---|---|---|---|"]
        for r in rows:
            contract = ("—" if not r["contract"] else
                        f"**breaks** ({', '.join(r['breaks'])})" if r["breaks"] else f"v{r['contract']['version']} kept")
            name = link(r["dataset"], r["model"]) if r["dataset"] else f"{r['model']} (not in the catalog yet)"
            out.append(f"| {name} | {r['change']} | {len(r['downstream'])} | {r['certified']} | {r['dashboards']} | "
                       f"{contract} |")
        for r in rows:
            if r["removed_columns"]:
                out += ["", f"**{r['model']}** removes columns: {', '.join(f'`{c}`' for c in r['removed_columns'])}."]
            top = [d for d in r["downstream"] if d["certified"]][:5] or r["downstream"][:5]
            if top:
                out.append(f"**{r['model']}** feeds {', '.join(link(d['urn'], d['name']) for d in top)}"
                           + (f" and {len(r['downstream']) - len(top)} more" if len(r["downstream"]) > len(top) else "")
                           + ".")
            if r["owners"]:
                out.append(f"People to tell about **{r['model']}**: {', '.join(p.split(':')[-1] for p in r['owners'][:10])}.")
        return "\n".join(out)

    def handle_webhook(self, rid: str, headers: dict, body: bytes) -> dict:
        repo = self.repo(rid)
        pr = pull_request_from(repo, headers, body)
        if pr is None:
            return {"ignored": True}
        return self.impact(rid, pr)

    # ---- docs back to dbt ---------------------------------------------------------------------------------
    def match(self, urn: str) -> Optional[dict]:
        """The repository and model behind a dataset, when one is configured."""
        for repo in self.repos.values():
            for candidate in ("dbt", repo.platform):
                m = re.match(rf"^urn:li:dataset:\(urn:li:dataPlatform:{re.escape(candidate)},(.+),{re.escape(repo.env)}\)$",
                             urn)
                if not m:
                    continue
                prefix = ".".join(p for p in (repo.database, repo.schema_) if p)
                name = m.group(1)
                if prefix and not name.lower().startswith(prefix.lower() + "."):
                    continue
                return {"repo": repo.id, "model": name.split(".")[-1]}
        return None

    def writeback(self, urn: str, actor: str) -> dict:
        found = self.match(urn)
        if found is None:
            raise GitError("no configured dbt repository builds this dataset", 404)
        repo = self.repo(found["repo"])
        model = found["model"]
        table = (self.graph.get_aspect(urn, "editableDatasetProperties") or {}).get("description")
        columns = {f["fieldPath"].split(".")[-1]: f["description"]
                   for f in (self.graph.get_aspect(urn, "editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []
                   if f.get("description")}
        if not table and not columns:
            raise GitError("nobody has written a description in Cairn for this dataset yet")
        prov = self.provider(repo)
        root = repo.dbt_project_dir.strip("/")
        candidates = [p for p in prov.tree(repo.base_branch, f"{root}/models" if root else "models")
                      if p.endswith((".yml", ".yaml"))][:300]
        target = None
        for path in candidates:
            text = prov.read(path, repo.base_branch) or ""
            if model in _models_in_yaml(text):
                target = (path, text)
                break
        if target is None:
            raise GitError(f"no properties file in {repo.repo} declares model {model}", 404)
        new, changed = set_descriptions(target[1], model, table, columns)
        if not changed:
            return {"repo": repo.id, "model": model, "file": target[0], "pull_request": None,
                    "note": "the dbt project already has these descriptions"}
        branch = f"cairn/docs-{model}-{int(time.time())}"
        title = f"docs({model}): descriptions from Cairn"
        body = (f"Descriptions written in Cairn for `{model}`"
                + (f" ({self.public_url}/cairn/asset/{quote(urn, safe='')})" if self.public_url else "")
                + f", requested by {actor.split(':')[-1]}.\n\n"
                + ("- the model's description\n" if table else "")
                + "".join(f"- column `{c}`\n" for c in columns))
        url = prov.open_pr(branch, {target[0]: new}, title, body)
        if self.audit is not None:
            self.audit.record("git", "git.writeback_opened", actor, urn, {"repo": repo.id, "pull_request": url})
        return {"repo": repo.id, "model": model, "file": target[0], "pull_request": url}
