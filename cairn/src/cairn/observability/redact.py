"""What telemetry may carry. Every span, span event and log record Cairn exports passes through here first, and
one that cannot be cleaned is dropped (fail closed): no credentials, no query parameter values, no SQL literals
(the values a query compares with, which may be the very values a mask hides), no request or response headers
beyond a short list, and no path segments that look like keys.

* **Headers** (``http.request.header.*``, ``http.response.header.*``): only :data:`SAFE_HEADERS` are kept.
* **Secret-looking names**: an attribute whose name says it holds a credential (``authorization``, ``cookie``,
  ``password``, ``secret``, ``token``, ``api_key``, ``credential``, ``session``, ``signature``) is replaced.
* **URLs** (``url.full``, ``http.url``, ``url.query``, ``http.target``, ``url.path``): user info and query
  values go; on a server span the path becomes the route template (``/v1/trino/{key}/allow``, never the key
  itself); otherwise path segments other than plain lowercase words become ``{}`` (a Slack webhook's secret
  path, an object key, an id).
* **SQL** (``db.statement``, ``db.query.text``, ``trino.query``): string, numeric and dollar-quoted literals
  become ``?``; comments go.
* **Everything else that is a string**: bearer tokens, JWTs, Cairn's own key formats, ``key=value`` pairs with a
  secret-sounding key, AWS keys and private-key blocks are replaced, and values are capped at 4,096 characters.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[redacted]"
MAX_VALUE = 4096
SAFE_HEADERS = frozenset({"content-type", "content-length", "user-agent", "accept", "x-request-id", "traceparent",
                          "x-trino-query-id", "x-forwarded-proto"})
_SECRET_NAME = re.compile(r"(?i)(authori[sz]ation|cookie|passw(or)?d|secret|token|api[_.-]?key|apikey|credential|"
                          r"session|signature|private[_.-]?key|access[_.-]?key|x-amz-security|bearer)")
_URL_KEYS = frozenset({"url.full", "http.url", "url.original"})
_PATH_KEYS = frozenset({"url.path", "http.target"})
_SQL_KEYS = frozenset({"db.statement", "db.query.text", "trino.query", "cairn.sql"})
_CREDENTIALS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{12,}"), "Basic " + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),         # a JWT
    (re.compile(r"\b(ca|cat|cpat|csk|ctk)_[A-Za-z0-9_-]{16,}"), REDACTED),                           # Cairn's key formats
    (re.compile(r"\b[0-9a-f]{32}:[A-Za-z0-9_-]{20,}"), REDACTED),                                     # a lakehouse session
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),                                           # an AWS access key
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"), REDACTED),
]
_SECRETS = [
    *_CREDENTIALS,
    (re.compile(r"(?i)\b([a-z0-9_.-]*(?:passw(?:or)?d|secret|token|api[_-]?key|apikey|credential|signature|"
                r"access[_-]?key|session[_-]?key)[a-z0-9_.-]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)"),
     r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),         # user:password@
    (re.compile(r"(?i)\b(PASSWORD\s+)(E?'(?:[^']|'')*')"), r"\1'" + REDACTED + "'"),                  # SQL: PASSWORD '...'
]
_SQL_STRING = re.compile(r"(?s)(?:\b[EeNnUuXxBb]&?)?'(?:[^']|'')*'")
_SQL_DOLLAR = re.compile(r"(?s)\$([A-Za-z_]*)\$.*?\$\1\$")
_SQL_NUMBER = re.compile(r"(?<![A-Za-z0-9_.\"$])[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-z0-9_\"])")
_SQL_COMMENT = re.compile(r"(?s)/\*.*?\*/|--[^\n]*")
_WORD = re.compile(r"^(v\d{1,2}|[a-z][a-z_.-]{0,31})$")


class Unredactable(ValueError):
    """A value Cairn cannot make safe to export: the span or record carrying it is dropped."""


def scrub_text(value: str) -> str:
    """Secrets out of free text (log messages, exception messages, status descriptions)."""
    for pattern, replacement in _SECRETS:
        value = pattern.sub(replacement, value)
    return value[:MAX_VALUE]


def scrub_all(value: str) -> str:
    """:func:`scrub_text` without its length limit, for a document whose size the caller limits itself."""
    for pattern, replacement in _SECRETS:
        value = pattern.sub(replacement, value)
    return value


def scrub_doc(value):
    """Secrets out of every string of a JSON document (its shape kept: a document is never cut mid-string)."""
    if isinstance(value, str):
        return scrub_all(value)
    if isinstance(value, list):
        return [scrub_doc(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_doc(v) for k, v in value.items()}
    return value


_SQLA_PARAMS = re.compile(r"(?s)\[parameters: .*?\](?=\s*(\(Background on this error|$|\n))")
_SQLA_SQL = re.compile(r"(?s)\[SQL: (.*?)\](?=\s*(\[parameters|\(Background|$|\n))")


def scrub_error(value: str) -> str:
    """An error message or a log line: secrets out, and the values a statement carried (SQLAlchemy's
    ``[parameters: ...]``, quoted literals) replaced, since an error may echo the very value a mask hides."""
    value = _SQLA_PARAMS.sub("[parameters: " + REDACTED + "]", value)
    value = _SQLA_SQL.sub(lambda m: "[SQL: " + scrub_sql(m.group(1)) + "]", value)
    value = _SQL_STRING.sub("'?'", value)
    return scrub_text(value)


def scrub_sql(sql: str) -> str:
    """The statement's shape without its values: literals become ``?``, comments go."""
    sql = _SQL_COMMENT.sub(" ", sql)
    sql = _SQL_DOLLAR.sub("?", sql)
    sql = _SQL_STRING.sub("?", sql)
    sql = _SQL_NUMBER.sub("?", sql)
    return scrub_text(re.sub(r"\s+", " ", sql).strip())


def scrub_query(query: str) -> str:
    """Query parameter names, without their values."""
    names = [p.split("=", 1)[0] for p in query.split("&") if p]
    return "&".join(f"{n}={REDACTED}" if not _SECRET_NAME.search(n) else f"{REDACTED}={REDACTED}"
                    for n in names[:32])


def scrub_path(path: str) -> str:
    """Path segments other than plain lowercase words become ``{}`` (ids, keys, object names, webhook secrets)."""
    head, _, query = path.partition("?")
    segs = [s if (not s or (_WORD.match(s) and not _SECRET_NAME.search(s))) else "{}" for s in head.split("/")]
    return "/".join(segs) + ("?" + scrub_query(query) if query else "")


def scrub_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        raise Unredactable("not a URL")
    return urlunsplit((parts.scheme, host, scrub_path(parts.path), scrub_query(parts.query) if parts.query else "", ""))


def _value(key: str, value: Any, server: bool, route: Optional[str]) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_value(key, v, server, route) for v in value)
    if isinstance(value, bytes):
        return REDACTED
    v = str(value)
    if key in _SQL_KEYS:
        return scrub_sql(v)
    if key in _URL_KEYS:
        out = scrub_url(v)
        if server and route:
            parts = urlsplit(out)
            out = urlunsplit((parts.scheme, parts.netloc, route, parts.query, ""))
        return out
    if key in _PATH_KEYS:
        return route if (server and route) else scrub_path(v)
    if key == "url.query":
        return scrub_query(v)
    return scrub_text(v)


def scrub_attributes(attrs: Optional[Mapping[str, Any]], server: bool = False) -> dict:
    """A cleaned copy of span or log attributes (see the module's rules). ``server``: the attributes of a span
    that served a request, whose path is replaced by its route template."""
    if not attrs:
        return {}
    route = attrs.get("http.route")
    out: dict = {}
    for key, value in attrs.items():
        k = str(key)
        low = k.lower()
        if low.startswith(("http.request.header.", "http.response.header.")):
            if low.split(".header.", 1)[1].replace("_", "-") not in SAFE_HEADERS:
                continue
        elif _SECRET_NAME.search(low.rsplit(".", 1)[-1]):
            out[k] = REDACTED
            continue
        out[k] = _value(low, value, server, str(route) if route else None)
    return out


def check_clean(values: Iterable[Any]) -> None:
    """Fail closed: raises when anything still looks like a credential after cleaning."""
    for v in values:
        if isinstance(v, (list, tuple)):
            check_clean(v)
        elif isinstance(v, str):
            for pattern, _ in _CREDENTIALS:
                if pattern.search(v):
                    raise Unredactable("a credential survived redaction")
