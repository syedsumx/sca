"""A declarative REST source (L165): any JSON API described in a few fields, loaded by a SaaS sync
(``"kind": "rest"``) into a governed lakehouse table, without a connector written for it.

A sync of this kind carries a ``spec``::

    {"records": "data.items",            # where the list of records is in the response ("" = the response is the list)
     "id": "id",                         # each record's id (a dotted path)
     "modified": "updated_at",           # its modification time (omit: the table is replaced at every run)
     "cursor_param": "updated_since",    # the query parameter that asks for records modified since the cursor
     "params": {"status": "active"},     # fixed query parameters
     "auth": {"type": "bearer"},         # bearer | basic | header | query | none ("name": the header or parameter)
     "pagination": {"type": "cursor", "param": "starting_after", "path": "meta.next"}}

``object`` is the path under the API's base (``v1/customers``) and ``fields`` are the record's fields to load
(dotted paths; ``address.city`` becomes the column ``address_city``). The base URL and the credentials live in the
sync's secret scope: ``base_url``, and ``token`` (bearer, header, query) or ``username`` and ``password`` (basic).

Pagination types: ``none``; ``cursor`` (the next page's token at ``path`` in the response, sent as ``param``);
``offset`` (``param`` advanced by the number of records, with ``limit_param`` = ``limit``); ``page`` (``param``
counted from ``start``, with ``size_param`` = ``size``); ``link`` (the next page's URL in the ``Link`` header's
``rel="next"``, or at ``path`` in the response).

**What Cairn will and will not do for a spec.** Only GET, only JSON. Every URL fetched, the next-page links
included, is checked before the request (https, a public address unless an operator allows private networks, no
credentials in it) and must stay on the base URL's origin, so a response cannot send the token to another host.
Redirects are not followed. A run stops at ``CAIRN_SAAS_REST_MAX_PAGES`` pages (10,000), a response over
``CAIRN_SAAS_REST_MAX_BYTES`` (50 MB) is refused, and 429 and 5xx are retried a few times, waiting as
``Retry-After`` says (at most a minute). Errors name the status, never the response's text or the credentials.

**Incremental or whole.** With ``modified`` and ``cursor_param``, a run asks for records modified since the cursor
and upserts them by id; the cursor moves to the newest modification time only when the whole run has loaded, since
an API need not return records in order. Without them the run reads everything and replaces the table.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urljoin, urlsplit

PATH = re.compile(r"^[A-Za-z0-9_.~-]+(/[A-Za-z0-9_.~-]+)*$")            # the resource's path under the base URL
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,5}$")
PARAM = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]{0,63}$")
HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
AUTH_TYPES = ("bearer", "basic", "header", "query", "none")
PAGE_TYPES = ("none", "cursor", "offset", "page", "link")
# Headers a spec may not set as its credential's header: they change where or how the request goes.
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie", "proxy-authorization"}


class RestSpecError(ValueError):
    pass


def _path(spec: dict, key: str, what: str, required: bool = False, allow_empty: bool = False) -> Optional[str]:
    v = spec.get(key)
    if v is None or (v == "" and not allow_empty):
        if required:
            raise RestSpecError(f"spec.{key}: {what}")
        return "" if allow_empty and v == "" else None
    if v == "" and allow_empty:
        return ""
    if not isinstance(v, str) or not FIELD.match(v):
        raise RestSpecError(f"spec.{key}: {what} (a dotted path of names)")
    return v


def _param(d: dict, key: str, where: str, default: Optional[str] = None) -> str:
    v = d.get(key, default)
    if not isinstance(v, str) or not PARAM.match(v):
        raise RestSpecError(f"{where}.{key}: a query parameter's name")
    return v


def _int(d: dict, key: str, where: str, default: int, low: int, high: int) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
        raise RestSpecError(f"{where}.{key}: a whole number from {low} to {high}")
    return v


def check_spec(spec: Any) -> dict:
    """The spec, validated and with its defaults filled in. Raises RestSpecError saying what is wrong."""
    if not isinstance(spec, dict):
        raise RestSpecError("spec: how to read the API (records, id, auth, pagination)")
    unknown = set(spec) - {"records", "id", "modified", "cursor_param", "params", "auth", "pagination"}
    if unknown:
        raise RestSpecError(f"spec: unknown keys {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {
        "records": _path(spec, "records", "where the list of records is in the response", allow_empty=True) or "",
        "id": _path({"id": spec.get("id", "id")}, "id", "each record's id", required=True),
        "modified": _path(spec, "modified", "each record's modification time"),
    }
    cursor_param = spec.get("cursor_param")
    if cursor_param is not None:
        cursor_param = _param(spec, "cursor_param", "spec")
    if bool(out["modified"]) != bool(cursor_param):
        raise RestSpecError("spec: modified and cursor_param go together (both for incremental runs; neither, and "
                            "each run replaces the table)")
    out["cursor_param"] = cursor_param
    params = spec.get("params") or {}
    if not isinstance(params, dict) or len(params) > 20 or not all(
            isinstance(k, str) and PARAM.match(k) and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 500
            for k, v in params.items()):
        raise RestSpecError("spec.params: up to 20 fixed query parameters (name: text or number)")
    out["params"] = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}

    auth = spec.get("auth") or {"type": "bearer"}
    if not isinstance(auth, dict) or auth.get("type") not in AUTH_TYPES or set(auth) - {"type", "name"}:
        raise RestSpecError(f"spec.auth: type is one of {', '.join(AUTH_TYPES)} (and name, for header and query)")
    a: dict[str, Any] = {"type": auth["type"]}
    if auth["type"] == "header":
        name = auth.get("name")
        if not isinstance(name, str) or not HEADER.match(name) or name.lower() in _FORBIDDEN_HEADERS:
            raise RestSpecError("spec.auth.name: the header that carries the key (e.g. X-Api-Key)")
        a["name"] = name
    elif auth["type"] == "query":
        a["name"] = _param(auth, "name", "spec.auth")
    elif "name" in auth:
        raise RestSpecError("spec.auth.name: only for header and query")
    out["auth"] = a

    page = spec.get("pagination") or {"type": "none"}
    if not isinstance(page, dict) or page.get("type") not in PAGE_TYPES:
        raise RestSpecError(f"spec.pagination: type is one of {', '.join(PAGE_TYPES)}")
    kind = page["type"]
    allowed = {"none": set(), "cursor": {"param", "path"}, "offset": {"param", "limit_param", "limit"},
               "page": {"param", "start", "size_param", "size"}, "link": {"path"}}[kind]
    if set(page) - allowed - {"type"}:
        raise RestSpecError(f"spec.pagination: {kind} takes {', '.join(sorted(allowed)) or 'nothing else'}")
    p: dict[str, Any] = {"type": kind}
    if kind == "cursor":
        p["param"] = _param(page, "param", "spec.pagination")
        p["path"] = _path(page, "path", "where the next page's token is in the response", required=True)
    elif kind == "offset":
        p["param"] = _param(page, "param", "spec.pagination", "offset")
        p["limit_param"] = _param(page, "limit_param", "spec.pagination", "limit")
        p["limit"] = _int(page, "limit", "spec.pagination", 100, 1, 10_000)
    elif kind == "page":
        p["param"] = _param(page, "param", "spec.pagination", "page")
        p["start"] = _int(page, "start", "spec.pagination", 1, 0, 1)
        p["size_param"] = _param(page, "size_param", "spec.pagination", "per_page")
        p["size"] = _int(page, "size", "spec.pagination", 100, 1, 10_000)
    elif kind == "link":
        p["path"] = _path(page, "path", "where the next page's URL is in the response")
    out["pagination"] = p
    used = [x for x in (out["cursor_param"], a.get("name") if a["type"] == "query" else None,
                        *(p.get(k) for k in ("param", "limit_param", "size_param"))) if x]
    clash = {x for x in used if used.count(x) > 1} | (set(used) & set(out["params"]))
    if clash:
        raise RestSpecError(f"spec: the parameter {sorted(clash)[0]} is used for two things")
    return out


def column(field: str) -> str:
    """The table column a field is loaded into."""
    return field.lower().replace(".", "_")


def dig(doc: Any, path: str) -> Any:
    """The value at a dotted path, or None."""
    if path == "":
        return doc
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v, separators=(",", ":"), sort_keys=True)
    return str(v)


def _origin(url: str) -> tuple[str, str, Optional[int]]:
    u = urlsplit(url)
    return u.scheme.lower(), (u.hostname or "").lower(), u.port or {"https": 443, "http": 80}.get(u.scheme.lower())


def _next_link(header: Optional[str]) -> Optional[str]:
    """The ``rel="next"`` target of a Link header."""
    for part in (header or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)', part)
        if m and re.search(r'rel\s*=\s*"?([^";]*\s)?next(\s[^";]*)?"?', m.group(2), re.I):
            return m.group(1)
    return None


def rest_records(http, secrets: dict[str, str], obj: str, fields: list[str], spec: dict, cursor: Optional[str],
                 check_url: Callable[[str, str], str], sleep: Callable[[float], None] = time.sleep,
                 error: type = RuntimeError) -> Iterator[dict]:
    """The API's records as ``{"id", "modified", "values"}``, page by page. ``check_url(url, what)`` refuses an
    address Cairn must not fetch; ``error(message, status)`` is raised for anything that stops the run."""
    max_pages = int(os.environ.get("CAIRN_SAAS_REST_MAX_PAGES", "10000"))
    max_bytes = int(os.environ.get("CAIRN_SAAS_REST_MAX_BYTES", str(50 << 20)))
    base = (secrets.get("base_url") or "").strip()
    if not base or urlsplit(base).query or urlsplit(base).fragment:
        raise error("rest: the secret base_url is the API's base address (https://api.example.com), without a query", 400)
    base = check_url(base.rstrip("/") + "/", "base_url")
    home = _origin(base)
    url = urljoin(base, obj)
    auth, page = spec["auth"], spec["pagination"]
    headers = {"Accept": "application/json", "User-Agent": "cairn-rest-source"}
    params: dict[str, str] = dict(spec["params"])
    basic = None
    if auth["type"] != "none":
        if auth["type"] == "basic":
            if not secrets.get("username") or secrets.get("password") is None:
                raise error("rest: basic auth needs the secrets username and password", 403)
            basic = (secrets["username"], secrets["password"])
        else:
            token = secrets.get("token")
            if not token:
                raise error("rest: the secret token is missing", 403)
            if auth["type"] == "bearer":
                headers["Authorization"] = f"Bearer {token}"
            elif auth["type"] == "header":
                headers[auth["name"]] = token
            else:
                params[auth["name"]] = token
    if cursor and spec["cursor_param"]:
        params[spec["cursor_param"]] = cursor
    if page["type"] == "offset":
        params[page["limit_param"]] = str(page["limit"])
        params[page["param"]] = "0"
    elif page["type"] == "page":
        params[page["size_param"]] = str(page["size"])
        params[page["param"]] = str(page["start"])
    offset, number, seen_tokens = 0, page.get("start", 1), set()
    send_params: Optional[dict] = params
    for _ in range(max_pages):
        check_url(url, "the API's address")
        if _origin(url) != home:
            raise error("rest: the next page is on another host than base_url; refused", 502)
        for attempt in range(4):
            try:
                r = http.get(url, params=send_params, headers=headers, auth=basic, follow_redirects=False)
            except Exception as e:                       # noqa: BLE001 - named by its kind only: a client's message
                # can carry the address, and with a key sent as a query parameter the address carries the key
                raise error(f"rest: could not reach the API ({type(e).__name__})", 502) from None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise error(f"rest: the API answered {r.status_code} four times", 502)
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                sleep(min(max(wait, 0.5 * (2 ** attempt)), 60.0))
                continue
            break
        if 300 <= r.status_code < 400:
            raise error(f"rest: the API redirected ({r.status_code}); give its final address in base_url", 502)
        if r.status_code in (401, 403):
            raise error(f"rest: the API refused the credentials ({r.status_code})", 502)
        if r.status_code != 200:
            raise error(f"rest: the API answered {r.status_code}", 502)
        if len(r.content) > max_bytes:
            raise error(f"rest: a response over {max_bytes} bytes; ask for smaller pages", 502)
        try:
            doc = r.json()
        except ValueError:
            raise error("rest: the API's answer is not JSON", 502)
        records = dig(doc, spec["records"])
        if records is None and spec["records"]:
            records = []
        if not isinstance(records, list):
            raise error(f"rest: no list of records at {spec['records'] or 'the top of the response'}", 502)
        for rec in records:
            if not isinstance(rec, dict):
                raise error("rest: a record is not an object", 502)
            rid = dig(rec, spec["id"])
            if rid is None or isinstance(rid, (dict, list)) or str(rid) == "":
                raise error(f"rest: a record has no {spec['id']}", 502)
            modified = _text(dig(rec, spec["modified"])) if spec["modified"] else ""
            yield {"id": str(rid), "modified": modified or "", "values": {f: _text(dig(rec, f)) for f in fields}}
        # ---- the next page ----
        kind = page["type"]
        if kind == "none" or not records and kind in ("offset", "page"):
            return
        if kind == "cursor":
            token = dig(doc, page["path"])
            if token is None or token == "" or token is False:
                return
            if isinstance(token, (dict, list)) or str(token) in seen_tokens:
                raise error("rest: the API gave the same next-page token twice", 502)
            seen_tokens.add(str(token))
            params[page["param"]] = str(token)
        elif kind == "offset":
            if len(records) < page["limit"]:
                return
            offset += len(records)
            params[page["param"]] = str(offset)
        elif kind == "page":
            if len(records) < page["size"]:
                return
            number += 1
            params[page["param"]] = str(number)
        else:                                            # link: the API gives the next page's whole URL
            nxt = dig(doc, page["path"]) if page.get("path") else _next_link(r.headers.get("Link"))
            if not nxt:
                return
            if not isinstance(nxt, str) or nxt in seen_tokens:
                raise error("rest: the API gave the same next page twice", 502)
            seen_tokens.add(nxt)
            url = urljoin(url, nxt)
            # The link carries its own query. Only a key sent as a query parameter is added to it again, into the
            # URL itself (a client's params would replace the link's query, and the same page would come back).
            send_params = None
            if auth["type"] == "query":
                from urllib.parse import parse_qsl, urlencode, urlunsplit

                u = urlsplit(url)
                query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != auth["name"]]
                url = urlunsplit((u.scheme, u.netloc, u.path, urlencode([*query, (auth["name"], params[auth["name"]])]), ""))
    raise error(f"rest: more than {max_pages} pages in one run (CAIRN_SAAS_REST_MAX_PAGES)", 502)


__all__ = ["RestSpecError", "check_spec", "column", "dig", "rest_records"]
