"""Knowledge sources for AI context: Confluence, Notion, Google Docs and SharePoint pages, linked to
the assets they talk about and used to ground the assistant and agents.

Sources are reviewed YAML files in ``config/knowledge`` (or ``CAIRN_KNOWLEDGE``); ``${ENV}`` values come
from the environment::

    sources:
      - {id: wiki, type: confluence, url: https://acme.atlassian.net, space: DATA,
         user: bot@acme.com, token: ${CONFLUENCE_TOKEN}}
      - {id: handbook, type: notion, database: 0f3c…, token: ${NOTION_TOKEN}}
      - {id: specs, type: gdocs, folder: 1AbC…}                 # the service's Google identity
      - {id: portal, type: sharepoint, site: <site id>, tenant_id: …, client_id: …, client_secret: ${…}}

The notifier syncs every source daily (``POST /context/knowledge/sync`` runs it now). A page is linked to
an asset when it names it: a link to the asset in Cairn, its full name (``schema.table`` or longer) or,
for distinctive names, the table name. Linked pages show as documents on the asset (and in Cairn
Core's links), and ``search_knowledge`` (assistant and MCP) finds pages by meaning to ground answers.

Only configured spaces, databases, folders and sites are read, and pages from a source are shown to
everyone in Cairn unless the source sets ``admins_only: true``: index what the whole organisation may
read. Page text is screened like other catalog text before an AI sees it.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time
from typing import Any, Callable, Iterable, Literal, Optional
from urllib.parse import unquote

import httpx
import yaml
from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, String, Table, Text, delete, select

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

knowledge_pages = Table(
    "cairn_knowledge_pages", metadata,
    Column("id", String(40), primary_key=True),
    Column("source", String(64), nullable=False, index=True),
    Column("title", String(500), nullable=False),
    Column("url", String(2000), nullable=False),
    Column("text", Text, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Column("linked", JSONText, nullable=False),          # dataset urns the page names
    Column("vector", JSONText, nullable=False),
    Column("synced_ms", BigInteger, nullable=False),
)
MAX_TEXT = 40_000
MAX_PAGES = 2000


class KnowledgeError(ValueError):
    pass


class KnowledgeSource(BaseModel):
    id: str
    type: Literal["confluence", "notion", "gdocs", "sharepoint"]
    url: Optional[str] = None
    space: Optional[str] = None
    database: Optional[str] = None
    folder: Optional[str] = None
    site: Optional[str] = None
    user: Optional[str] = None
    token: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    admins_only: bool = False


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    return v


def load_sources(path: Optional[str] = None) -> dict[str, KnowledgeSource]:
    path = path or os.environ.get("CAIRN_KNOWLEDGE", "/etc/cairn/config/knowledge")
    if not path or not os.path.exists(path):
        return {}
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")) and ".example." not in f)
    out: dict[str, KnowledgeSource] = {}
    for f in files:
        with open(f) as fh:
            for raw in (yaml.safe_load(fh) or {}).get("sources") or []:
                s = KnowledgeSource.model_validate({k: _expand(v) for k, v in raw.items()})
                out[s.id] = s
    return out


def html_text(markup: str) -> str:
    """Readable text from Confluence storage format or SharePoint web parts."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup or "")
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^\S\n]+", " ", html.unescape(t))          # any run of spaces (incl. no-break) → one
    t = "\n".join(line.strip() for line in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _ms(ts: Optional[str]) -> int:
    from datetime import datetime

    if not ts:
        return int(time.time() * 1000)
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


# ---- fetchers: each yields {id, title, url, text, updated_ms} ------------------------------------------
class Fetcher:
    def __init__(self, src: KnowledgeSource, http: httpx.Client):
        self.s = src
        self.http = http

    def _get(self, url: str, **kw) -> dict:
        r = self.http.get(url, **kw)
        if r.status_code >= 400:
            raise KnowledgeError(f"{self.s.type} said {r.status_code}: {r.text[:200]}")
        return r.json()


class Confluence(Fetcher):
    def pages(self) -> Iterable[dict]:
        base = (self.s.url or "").rstrip("/")
        auth = (self.s.user or "", self.s.token)
        start = 0
        while start < MAX_PAGES:
            data = self._get(f"{base}/wiki/rest/api/content", auth=auth, params={
                "spaceKey": self.s.space, "type": "page", "expand": "body.storage,version", "limit": 50, "start": start})
            for p in data.get("results") or []:
                yield {"id": p["id"], "title": p.get("title") or "Untitled",
                       "url": f"{base}/wiki{(p.get('_links') or {}).get('webui', '/pages/' + p['id'])}",
                       "text": html_text(((p.get("body") or {}).get("storage") or {}).get("value", "")),
                       "updated_ms": _ms((p.get("version") or {}).get("when"))}
            if not (data.get("_links") or {}).get("next"):
                return
            start += 50


class Notion(Fetcher):
    API = "https://api.notion.com/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.token}", "Notion-Version": "2022-06-28"}

    def _text(self, block_id: str, depth: int = 0) -> str:
        parts, cursor = [], None
        while True:
            data = self._get(f"{self.API}/blocks/{block_id}/children", headers=self._headers(),
                             params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            for b in data.get("results") or []:
                rich = (b.get(b.get("type") or "") or {}).get("rich_text") or []
                line = "".join(t.get("plain_text", "") for t in rich)
                if line:
                    parts.append(line)
                if b.get("has_children") and depth < 2:
                    parts.append(self._text(b["id"], depth + 1))
            if not data.get("has_more"):
                return "\n".join(p for p in parts if p)
            cursor = data.get("next_cursor")

    def pages(self) -> Iterable[dict]:
        cursor, n = None, 0
        while n < MAX_PAGES:
            r = self.http.post(f"{self.API}/databases/{self.s.database}/query", headers=self._headers(),
                               json={"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            if r.status_code >= 400:
                raise KnowledgeError(f"notion said {r.status_code}: {r.text[:200]}")
            data = r.json()
            for p in data.get("results") or []:
                title_prop = next((v for v in (p.get("properties") or {}).values() if v.get("type") == "title"), {})
                title = "".join(t.get("plain_text", "") for t in title_prop.get("title") or []) or "Untitled"
                n += 1
                yield {"id": p["id"], "title": title, "url": p.get("url") or f"https://www.notion.so/{p['id'].replace('-', '')}",
                       "text": self._text(p["id"]), "updated_ms": _ms(p.get("last_edited_time"))}
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")


def _google_token(http: httpx.Client) -> str:
    if os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
        return os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]
    r = http.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                 headers={"Metadata-Flavor": "Google"})
    r.raise_for_status()
    return r.json()["access_token"]


class GoogleDocs(Fetcher):
    def pages(self) -> Iterable[dict]:
        headers = {"Authorization": f"Bearer {self.s.token or _google_token(self.http)}"}
        token, n = None, 0
        while n < MAX_PAGES:
            data = self._get("https://www.googleapis.com/drive/v3/files", headers=headers, params={
                "q": f"'{self.s.folder}' in parents and mimeType='application/vnd.google-apps.document' and trashed=false",
                "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink)", "pageSize": 100,
                **({"pageToken": token} if token else {})})
            for f in data.get("files") or []:
                r = self.http.get(f"https://www.googleapis.com/drive/v3/files/{f['id']}/export",
                                  params={"mimeType": "text/plain"}, headers=headers)
                if r.status_code >= 400:
                    continue
                n += 1
                yield {"id": f["id"], "title": f.get("name") or "Untitled",
                       "url": f.get("webViewLink") or f"https://docs.google.com/document/d/{f['id']}",
                       "text": r.text, "updated_ms": _ms(f.get("modifiedTime"))}
            token = data.get("nextPageToken")
            if not token:
                return


class SharePoint(Fetcher):
    GRAPH = "https://graph.microsoft.com/v1.0"

    def _token(self) -> str:
        r = self.http.post(f"https://login.microsoftonline.com/{self.s.tenant_id}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.s.client_id, "client_secret": self.s.client_secret,
            "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise KnowledgeError(f"Entra ID refused the credentials: {r.text[:200]}")
        return r.json()["access_token"]

    def pages(self) -> Iterable[dict]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        url: Optional[str] = f"{self.GRAPH}/sites/{self.s.site}/pages/microsoft.graph.sitePage?$expand=canvasLayout"
        n = 0
        while url and n < MAX_PAGES:
            data = self._get(url, headers=headers)
            for p in data.get("value") or []:
                parts = [w.get("innerHtml") or "" for section in ((p.get("canvasLayout") or {}).get("horizontalSections") or [])
                         for col in section.get("columns") or [] for w in col.get("webparts") or []]
                n += 1
                yield {"id": p["id"], "title": p.get("title") or p.get("name") or "Untitled", "url": p.get("webUrl") or "",
                       "text": html_text("\n".join(parts)), "updated_ms": _ms(p.get("lastModifiedDateTime"))}
            url = data.get("@odata.nextLink")


FETCHERS = {"confluence": Confluence, "notion": Notion, "gdocs": GoogleDocs, "sharepoint": SharePoint}


# ---- linking ---------------------------------------------------------------------------------------------
def dataset_names(urn: str) -> tuple[str, str]:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[^,]+\)$", urn)
    name = (m.group(1) if m else urn).lower()
    return name, name.split(".")[-1]


def links_in(text: str, datasets: list[str]) -> list[str]:
    """Datasets a page names: a Cairn asset link, the full name (``schema.table`` or longer), or a
    distinctive table name (with ``_`` and at least 8 characters, named more than once)."""
    low = text.lower()
    found = set()
    for m in re.finditer(r"/cairn/asset/(urn(?:%3a|:)li(?:%3a|:)dataset[^\s\"'<>]+)", text, re.I):
        urn = unquote(m.group(1)).rstrip(".,;:")
        while urn.count(")") > urn.count("("):                # a link inside (parentheses)
            urn = urn[:-1]
        found.add(urn)
    leaf_counts: dict[str, list[str]] = {}
    for urn in datasets:
        full, leaf = dataset_names(urn)
        parts = full.split(".")
        tail = ".".join(parts[-2:]) if len(parts) >= 2 else full
        if re.search(rf"(?<![\w.]){re.escape(tail)}(?![\w])", low):
            found.add(urn)
            continue
        leaf_counts.setdefault(leaf, []).append(urn)
    for leaf, urns in leaf_counts.items():
        if len(urns) == 1 and "_" in leaf and len(leaf) >= 8 and len(re.findall(rf"(?<![\w.]){re.escape(leaf)}(?!\w)", low)) >= 2:
            found.add(urns[0])
    return sorted(u for u in found if u in set(datasets))


# ---- the service -------------------------------------------------------------------------------------------
class KnowledgeService:
    def __init__(self, engine, sources: dict[str, KnowledgeSource], embed: Callable[[str], list[float]],
                 datasets: Callable[[], list[str]], graph=None, audit=None, public_url: str = "",
                 http: Optional[httpx.Client] = None):
        self.engine = engine
        self.sources = sources
        self.embed = embed
        self.datasets = datasets
        self.graph = graph
        self.audit = audit
        self.public_url = public_url
        self.http = http or httpx.Client(timeout=30)

    def sync(self, only: Optional[str] = None, actor: str = "urn:li:corpuser:cairn-knowledge") -> dict:
        out: dict[str, Any] = {}
        known = self.datasets()
        for sid, src in self.sources.items():
            if only and sid != only:
                continue
            try:
                pages = list(FETCHERS[src.type](src, self.http).pages())
            except Exception as e:  # noqa: BLE001 - one source failing does not stop the others
                logger.warning("knowledge %s: %s", sid, e)
                out[sid] = {"error": str(e)[:300]}
                continue
            now = int(time.time() * 1000)
            linked_total = 0
            ids = []
            with self.engine.begin() as conn:
                for p in pages:
                    pid = hashlib.sha1(f"{sid}|{p['id']}".encode()).hexdigest()[:40]
                    ids.append(pid)
                    text = (p["text"] or "")[:MAX_TEXT]
                    linked = links_in(f"{p['title']}\n{text}", known)
                    linked_total += len(linked)
                    conn.execute(delete(knowledge_pages).where(knowledge_pages.c.id == pid))
                    conn.execute(knowledge_pages.insert().values(
                        id=pid, source=sid, title=p["title"][:500], url=p["url"][:2000], text=text,
                        updated_ms=p["updated_ms"], linked=linked, vector=self.embed(f"{p['title']}\n{text[:8000]}"),
                        synced_ms=now))
                gone = conn.execute(delete(knowledge_pages).where(knowledge_pages.c.source == sid,
                                                                  knowledge_pages.c.id.not_in(ids or [""]))).rowcount
            self._publish_links(sid)
            out[sid] = {"pages": len(pages), "links": linked_total, "removed": gone}
        if self.audit is not None:
            self.audit.record("context", "knowledge.synced", actor, None, out)
        return out

    def _publish_links(self, sid: str) -> None:
        """Linked pages as documents on the asset in Cairn Core (only links Cairn added are touched)."""
        if self.graph is None:
            return
        from cairn.common.proposals import audit_stamp, make_mcp

        by_urn: dict[str, list[dict]] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(knowledge_pages)):
                for urn in r.linked or []:
                    by_urn.setdefault(urn, []).append({"url": r.url, "title": r.title, "source": r.source})
        mcps = []
        targets = set(by_urn)
        for urn in targets:
            current = self.graph.get_aspect(urn, "institutionalMemory") or {"elements": []}
            mine = [e for e in current.get("elements") or [] if (e.get("description") or "").endswith(" · knowledge")]
            others = [e for e in current.get("elements") or [] if e not in mine]
            wanted = [{"url": p["url"], "description": f"{p['title']} · knowledge",
                       "createStamp": audit_stamp("urn:li:corpuser:cairn-knowledge")} for p in by_urn[urn][:20]]
            if [(e["url"], e["description"]) for e in mine] != [(e["url"], e["description"]) for e in wanted]:
                mcps.append(make_mcp(urn, "institutionalMemory", {"elements": others + wanted}))
        if mcps:
            self.graph.emit(mcps)

    def _visible(self, is_admin: bool) -> set[str]:
        return {sid for sid, s in self.sources.items() if is_admin or not s.admins_only}

    def search(self, query: str, is_admin: bool = False, limit: int = 5) -> list[dict]:
        allowed = self._visible(is_admin)
        if not allowed or not query.strip():
            return []
        qv = self.embed(query)
        with self.engine.connect() as conn:
            rows = conn.execute(select(knowledge_pages).where(knowledge_pages.c.source.in_(allowed))).all()

        def cos(a, b):
            na = sum(x * x for x in a) ** 0.5
            nb = sum(x * x for x in b) ** 0.5
            return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
        scored = sorted(((cos(qv, r.vector), r) for r in rows), key=lambda s: -s[0])[:max(1, min(limit, 20))]
        return [{"title": r.title, "url": r.url, "source": r.source, "score": round(score, 4),
                 "excerpt": _excerpt(r.text, query), "datasets": r.linked}
                for score, r in scored if score > 0.05]

    def for_dataset(self, urn: str, is_admin: bool = False) -> list[dict]:
        allowed = self._visible(is_admin)
        with self.engine.connect() as conn:
            rows = conn.execute(select(knowledge_pages).where(knowledge_pages.c.source.in_(allowed or {""}))).all()
        return [{"title": r.title, "url": r.url, "source": r.source, "updated_ms": r.updated_ms}
                for r in rows if urn in (r.linked or [])]

    def overview(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(knowledge_pages.c.source, knowledge_pages.c.synced_ms,
                                       knowledge_pages.c.linked)).all()
        out = []
        for sid, s in self.sources.items():
            mine = [r for r in rows if r.source == sid]
            out.append({"id": sid, "type": s.type, "pages": len(mine), "linked_pages": sum(1 for r in mine if r.linked),
                        "synced_ms": max((r.synced_ms for r in mine), default=None), "admins_only": s.admins_only})
        return out


def _excerpt(text: str, query: str, width: int = 400) -> str:
    words = [w for w in re.findall(r"\w{4,}", query.lower())]
    low = text.lower()
    at = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, at - width // 4)
    return ("…" if start else "") + text[start:start + width].strip() + ("…" if start + width < len(text) else "")


__all__ = ["KnowledgeService", "KnowledgeSource", "load_sources", "links_in", "html_text"]
