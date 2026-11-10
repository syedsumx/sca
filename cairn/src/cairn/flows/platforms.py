"""Flow steps that drive other platforms: any REST API (``http``), Databricks jobs, Snowflake SQL and BigQuery jobs.

Each is a thin layer over one core (:class:`Remote`): the request goes out from Cairn, as the flow's owner, with
credentials read from a Cairn secret scope **as the owner** at the moment they are needed (never stored in the flow,
its run, its output or its log), TLS always verified (the system's CAs, or a private CA kept in the same scope), and,
when egress is enforced (``CAIRN_EGRESS_ENFORCE``), through Cairn's egress proxy with the owner's workspace's rules, so
a host no rule allows is refused there. A missing scope, key or grant fails the step before anything is sent.

Long work is started once and then looked at every ``poll_s`` without holding a worker (the step waits, like an
approval): its handle (a Databricks run id, a Snowflake statement handle, a BigQuery job id, a status URL) is kept in
the step's state, which is also what cancelling uses: a run that is cancelled, or a step that runs out of time,
cancels the remote work too (Databricks ``runs/cancel``, Snowflake ``/cancel``, BigQuery ``jobs.cancel``). Starts are
idempotent where the platform allows it (Databricks ``idempotency_token``, BigQuery's own job id, Snowflake's
``requestId``), so a Cairn process that stops between asking and recording does not start the work twice.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import json
import logging
import re
import socket
import ssl
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

from cairn.flows.steps import Backends, Ctx, Done, StepFailed, Wait

logger = logging.getLogger(__name__)
USER_AGENT = "Cairn-Flows/1.0"
MISSING = object()
TIMEOUT = 60.0
MAX_BODY = 20_000
# Headers that carry credentials: they come from a secret scope (auth), never from the flow's text.
CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-auth-token"}


# ---- JSONPath (the subset flows need) ---------------------------------------------------------------------
_JP = re.compile(r"\.([A-Za-z_$][A-Za-z0-9_$-]*)|\[\s*(-?\d+)\s*\]|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\]|(\[\s*\*\s*\]|\.\*)")


def _jp_parts(path: str) -> list[tuple[str, Any]]:
    p = (path or "").strip()
    if not p.startswith("$"):
        raise ValueError(f"{path!r} is not a JSONPath: it starts with $ ($.status, $.items[0].id, $['a b'], $.jobs[*].id)")
    out, pos, rest = [], 0, p[1:]
    while pos < len(rest):
        m = _JP.match(rest, pos)
        if not m:
            raise ValueError(f"{path!r} is not a JSONPath ($.status, $.items[0].id, $['a b'], $.jobs[*].id)")
        pos = m.end()
        if m.group(5):
            out.append(("*", None))
        elif m.group(2) is not None:
            out.append(("i", int(m.group(2))))
        else:
            out.append(("k", next(g for g in (m.group(1), m.group(3), m.group(4)) if g is not None)))
    return out


def check_jsonpath(path: str) -> Optional[str]:
    """None when ``path`` is a JSONPath this module reads, else why not."""
    try:
        _jp_parts(path)
        return None
    except ValueError as e:
        return str(e)


def jsonpath(doc: Any, path: str) -> Any:
    """The value at ``path`` in ``doc`` (a list for a path with ``[*]``), or :data:`MISSING`."""
    cur, many = [doc], False
    for kind, arg in _jp_parts(path):
        nxt: list = []
        for c in cur:
            if kind == "*":
                many = True
                nxt += list(c.values()) if isinstance(c, dict) else list(c) if isinstance(c, list) else []
            elif kind == "i":
                if isinstance(c, list) and -len(c) <= arg < len(c):
                    nxt.append(c[arg])
            elif isinstance(c, dict) and arg in c:
                nxt.append(c[arg])
        cur = nxt
    if many:
        return cur
    return cur[0] if cur else MISSING


_PLACEHOLDER = re.compile(r"\{(\$[^{}]*)\}")


def fill(text: str, doc: Any) -> str:
    """``{$.path}`` in ``text`` replaced with that value of ``doc`` (URL-quoted), e.g. a status URL from a first answer."""
    def one(m: re.Match) -> str:
        v = jsonpath(doc, m.group(1))
        if v is MISSING or isinstance(v, (dict, list)):
            raise StepFailed(f"{m.group(1)} is not in the first answer, so the status URL cannot be made")
        return quote(str(v), safe="")
    return _PLACEHOLDER.sub(one, text)


def placeholders(text: str) -> list[str]:
    return [m.group(1) for m in _PLACEHOLDER.finditer(text or "")]


# ---- JWTs (Snowflake key pairs, Google service accounts) -----------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_private_key(pem: str, passphrase: Optional[str], what: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        key = serialization.load_pem_private_key(pem.encode(), password=passphrase.encode() if passphrase else None)
    except Exception as e:  # noqa: BLE001 - never echo the key
        raise StepFailed(f"{what} is not a PEM private key the passphrase opens ({type(e).__name__})")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise StepFailed(f"{what} is not an RSA key")
    return key


def sign_rs256(key: Any, claims: dict, header: Optional[dict] = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", **(header or {})}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


# ---- the core ------------------------------------------------------------------------------------------------
def _loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def safe_url(url: str) -> str:
    """A URL for a log or an error: scheme, host and path (no query, which may carry tokens)."""
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}{(':' + str(u.port)) if u.port else ''}{u.path}"


class _Tokens:
    """Access tokens Cairn got with a secret (OAuth client credentials, a service account), kept in memory until a
    minute before they expire, by a digest of what got them (never the secret itself)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._got: dict[str, tuple[str, float]] = {}

    def get(self, key: str, fetch: Callable[[], tuple[str, int]]) -> str:
        with self._lock:
            hit = self._got.get(key)
            if hit and hit[1] > time.time():
                return hit[0]
        token, ttl = fetch()
        with self._lock:
            self._got[key] = (token, time.time() + max(0, ttl - 60))
            if len(self._got) > 1000:
                self._got.clear()
        return token


TOKENS = _Tokens()


def digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


class Remote:
    """One step's way out: secrets as the owner, verified TLS, egress rules, and nothing secret in what it says."""

    def __init__(self, b: Backends, ctx: Ctx):
        self.b, self.ctx = b, ctx
        self._secrets: set[str] = set()

    # -- secrets
    def secret(self, scope: Optional[str], key: Optional[str], what: str) -> str:
        if not scope or not key:
            raise StepFailed(f"{what} comes from a secret scope: set secret_scope and the key that holds it")
        if self.b.secrets is None:
            raise StepFailed("secret scopes are not set up in this Cairn (CAIRN_SECRETS_KEY), so the step cannot sign in")
        try:
            value = self.b.secrets.read(self.ctx.owner, scope, key)
        except Exception as e:  # noqa: BLE001 - SecretError (404/403/503): fail closed, say which and why
            why = str(e) or type(e).__name__
            raise StepFailed(f"{what}: the flow's owner cannot read {key!r} in the secret scope {scope!r} ({why}); "
                             "the owner needs READ on the scope")
        if not isinstance(value, str) or not value.strip():
            raise StepFailed(f"{what}: {key!r} in the secret scope {scope!r} is empty")
        value = value.strip()
        self._secrets.add(value)
        for line in value.splitlines():                      # a PEM's lines, too
            if len(line) >= 8:
                self._secrets.add(line)
        return value

    def scrub(self, text: Any) -> str:
        out = str(text)
        for s in sorted(self._secrets, key=len, reverse=True):
            if s and s in out:
                out = out.replace(s, "***")
        return out

    # -- transport
    def _verify(self, ca_pem: Optional[str]) -> Any:
        if ca_pem:
            try:
                return ssl.create_default_context(cadata=ca_pem)        # that CA only, host names checked
            except (ssl.SSLError, ValueError) as e:
                raise StepFailed(f"the CA certificate is not PEM ({type(e).__name__})")
        return True

    def _proxy(self) -> Optional[str]:
        eg = self.b.egress
        if eg is None or not getattr(eg, "enforced", False):
            return None
        try:
            env = eg.proxy_env(eg.workspace_of(self.ctx.owner), f"flow:{self.ctx.run_id}:{self.ctx.step.get('name', '')}",
                               max(300, min(int(self.ctx.step.get("timeout_s") or 3600), 86400)))
        except Exception as e:  # noqa: BLE001 - enforced but no proxy or key: nothing goes out
            raise StepFailed(f"egress is enforced and the egress proxy cannot be used: {e}")
        return env.get("HTTPS_PROXY") or None

    def client(self, ca_pem: Optional[str] = None, proxy: Optional[str] = None) -> Any:
        import httpx

        if proxy:                                           # every request through the proxy, whatever the URL
            return httpx.Client(verify=self._verify(ca_pem), proxy=proxy, trust_env=False, timeout=TIMEOUT,
                                follow_redirects=False, headers={"User-Agent": USER_AGENT})
        return httpx.Client(verify=self._verify(ca_pem), timeout=TIMEOUT, follow_redirects=False,
                            headers={"User-Agent": USER_AGENT})

    @staticmethod
    def _addresses(host: str, port: int) -> None:
        """Without the egress proxy, the request leaves from Cairn's own process: a host that resolves to a link-local
        address (the cloud metadata service) is always refused, and one on a private, loopback or reserved network
        unless CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS=true (Cairn's own services live there)."""
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise StepFailed(f"{host} could not be resolved")
        allow = os.environ.get("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS", "").lower() in ("1", "true", "yes")
        for info in infos:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            if ip.is_link_local or (ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_link_local):
                raise StepFailed(f"{host} resolves to a link-local address ({ip}): flows never call it")
            if not allow and (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
                raise StepFailed(f"{host} resolves to a private address ({ip}); an operator allows flows to call private "
                                 "networks with CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS=true")

    def call(self, method: str, url: str, *, headers: Optional[dict] = None, json_body: Any = MISSING,
             data: Optional[dict] = None, auth: Optional[tuple] = None, ca_pem: Optional[str] = None,
             credentials: bool = True, what: str = "") -> Any:
        """One request; StepFailed when it cannot be made (refused by egress, TLS, the network). The answer is
        returned whatever its status: the caller decides."""
        import httpx

        u = urlsplit(url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise StepFailed(f"{what or 'the URL'} is an https:// URL")
        if u.username or u.password:
            raise StepFailed(f"{what or 'the URL'} has credentials in it: use auth with a secret scope")
        if u.scheme == "http" and credentials and not _loopback(u.hostname):
            raise StepFailed(f"{what or 'the URL'} is plain http: credentials go only over TLS (https://)")
        kw: dict[str, Any] = {"headers": headers or {}}
        if json_body is not MISSING:
            kw["json"] = json_body
        if data is not None:
            kw["data"] = data
        if auth is not None:
            kw["auth"] = auth
        proxy = self._proxy()
        if not proxy:
            self._addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        try:
            with self.client(ca_pem, proxy) as c:
                return c.request(method, url, **kw)
        except httpx.ProxyError as e:
            raise StepFailed(f"egress refused {u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)} "
                             f"(no egress rule allows it for the owner's workspace): {self.scrub(e)}")
        except httpx.ConnectError as e:
            text = self.scrub(e)
            if "403" in text or "Forbidden" in text:
                raise StepFailed(f"egress refused {u.hostname}: no egress rule allows it for the owner's workspace")
            if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
                raise StepFailed(f"{safe_url(url)}: its TLS certificate is not trusted ({text[:200]})")
            raise StepFailed(f"{safe_url(url)} could not be reached: {text[:200]}")
        except httpx.TimeoutException:
            raise StepFailed(f"{safe_url(url)} did not answer within {int(TIMEOUT)} s")
        except httpx.HTTPError as e:
            raise StepFailed(f"{safe_url(url)}: {type(e).__name__}: {self.scrub(e)[:200]}")

    def failed(self, r: Any, what: str) -> StepFailed:
        try:
            doc = r.json()
        except ValueError:
            doc = r.text
        why: Any = doc
        if isinstance(doc, dict):
            why = doc.get("message") or doc.get("error_description") or doc.get("error") or doc.get("detail") or doc
            if isinstance(why, dict):
                why = why.get("message") or json.dumps(why)[:300]
        return StepFailed(f"{what} answered {r.status_code}: {self.scrub(str(why))[:400]}")


def body_of(r: Any) -> Any:
    """A response's JSON, or its text (bounded)."""
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text[:MAX_BODY]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _later(ctx: Ctx, state: dict, log: str = "") -> Wait:
    s = ctx.step
    return Wait(min(_now_ms() + int(s.get("poll_s") or 15) * 1000, max(ctx.deadline_ms, _now_ms() + 1000)), state, log)


def _overdue(b: Backends, ctx: Ctx, kind: str) -> None:
    """Out of time: the remote work is cancelled, then the step fails."""
    if _now_ms() > ctx.deadline_ms and ctx.state:
        cancel(b, kind, ctx.owner, ctx.state, ctx.run_id, ctx.flow)
        raise StepFailed(f"timed out after {ctx.step.get('timeout_s')} s; the remote work was cancelled")


# ---- http ------------------------------------------------------------------------------------------------------
def http_auth(r: Remote, s: dict) -> tuple[dict, Optional[tuple]]:
    """(headers, basic auth) for an http step's ``auth``."""
    kind = s.get("auth") or "none"
    if kind == "none":
        return {}, None
    value = r.secret(s.get("secret_scope"), s.get("secret_key"), f"the {kind} credential")
    if kind == "bearer":
        return {"Authorization": f"Bearer {value}"}, None
    if kind == "basic":
        return {}, (str(s.get("username") or ""), value)
    return {str(s["auth_header"]): value}, None


def _status_ok(s: dict, code: int) -> bool:
    ok = s.get("ok_status") or []
    return code in [int(x) for x in ok] if ok else 200 <= code < 300


def _http_ca(r: Remote, s: dict) -> Optional[str]:
    return r.secret(s.get("secret_scope"), s["ca_key"], "the CA certificate") if s.get("ca_key") else None


def run_http(b: Backends, ctx: Ctx) -> Any:
    s, st = ctx.step, ctx.state
    r = Remote(b, ctx)
    headers, basic = http_auth(r, s)
    headers = {**{str(k): str(v) for k, v in (s.get("headers") or {}).items()}, **headers}
    ca = _http_ca(r, s)
    secret_sent = (s.get("auth") or "none") != "none"
    if not st.get("poll_url"):
        method = s.get("method") or "GET"
        body = s.get("body")
        resp = r.call(method, s["url"], headers=headers, auth=basic, ca_pem=ca, credentials=secret_sent,
                      json_body=body if body is not None and method != "GET" else MISSING, what="url")
        if not _status_ok(s, resp.status_code):
            raise r.failed(resp, f"{method} {safe_url(s['url'])}")
        doc = body_of(resp)
        ctx.log(f"{method} {safe_url(s['url'])}: {resp.status_code}")
        if not s.get("status_path"):
            return Done(_http_output(r, s, resp.status_code, doc))
        poll = s.get("poll_url") or resp.headers.get("location") or s["url"]
        if poll.startswith("/"):
            u = urlsplit(s["url"])
            poll = f"{u.scheme}://{u.netloc}{poll}"
        poll = fill(poll, doc) if isinstance(doc, (dict, list)) else poll
        if urlsplit(poll).hostname != urlsplit(s["url"]).hostname and secret_sent:
            raise StepFailed(f"the status URL {safe_url(poll)} is on another host than url: the credential is not sent "
                             "there; set poll_url on the same host")
        ctx.log(f"looking at {safe_url(poll)} every {s.get('poll_s', 15)} s")
        return _later(ctx, {"poll_url": poll, "looks": 0, "first_status": resp.status_code})
    if _now_ms() > ctx.deadline_ms:
        raise StepFailed(f"timed out after {s.get('timeout_s')} s waiting for {s['status_path']}")
    resp = r.call("GET", st["poll_url"], headers=headers, auth=basic, ca_pem=ca, credentials=secret_sent,
                  what="the status URL")
    looks = int(st.get("looks", 0)) + 1
    if resp.status_code in (429, 502, 503, 504):
        return _later(ctx, {**st, "looks": looks}, f"the status URL answered {resp.status_code}; looking again")
    if not 200 <= resp.status_code < 300:
        raise r.failed(resp, f"GET {safe_url(st['poll_url'])}")
    doc = body_of(resp)
    v = jsonpath(doc, s["status_path"]) if isinstance(doc, (dict, list)) else MISSING
    shown = "(missing)" if v is MISSING else str(v)
    if shown in [str(x) for x in s.get("failure_values") or []]:
        raise StepFailed(f"{s['status_path']} is {r.scrub(shown)[:200]}: the remote work failed")
    if shown in [str(x) for x in s.get("success_values") or []]:
        ctx.log(f"{s['status_path']} is {r.scrub(shown)[:200]} after {looks} looks")
        out = _http_output(r, s, resp.status_code, doc)
        out["polls"] = looks
        return Done(out)
    return _later(ctx, {**st, "looks": looks, "last": shown[:200]}, f"{s['status_path']} is {r.scrub(shown)[:200]}")


def _http_output(r: Remote, s: dict, code: int, doc: Any) -> dict:
    out: dict[str, Any] = {"status": code}
    if isinstance(doc, (dict, list)):
        text = json.dumps(doc, default=str)
        out["body"] = json.loads(r.scrub(text)) if len(text) <= MAX_BODY else {"truncated": True,
                                                                               "text": r.scrub(text[:MAX_BODY])}
    elif doc is not None:
        out["body"] = r.scrub(doc)[:MAX_BODY]
    if s.get("output_path"):
        if not isinstance(doc, (dict, list)):
            raise StepFailed(f"output_path {s['output_path']}: the answer is not JSON")
        v = jsonpath(doc, s["output_path"])
        if v is MISSING:
            raise StepFailed(f"output_path {s['output_path']} is not in the answer")
        out["result"] = v
    return out


# ---- Databricks (Jobs API 2.2) --------------------------------------------------------------------------------
DBX_RUNNING = ("PENDING", "QUEUED", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY")


def _dbx_host(host: str) -> str:
    h = host.strip().rstrip("/")
    if "://" not in h:
        h = "https://" + h
    return h


def dbx_headers(r: Remote, st: dict) -> dict:
    """Databricks' Authorization header: a personal access token, or an OAuth M2M token (client credentials)."""
    host, scope = st["host"], st["scope"]
    if st.get("auth", "pat") == "pat":
        return {"Authorization": "Bearer " + r.secret(scope, st.get("token_key") or "token", "the Databricks token")}
    cid = r.secret(scope, st.get("client_id_key") or "client_id", "the Databricks OAuth client id")
    sec = r.secret(scope, st.get("client_secret_key") or "client_secret", "the Databricks OAuth client secret")

    def fetch() -> tuple[str, int]:
        resp = r.call("POST", f"{host}/oidc/v1/token", data={"grant_type": "client_credentials", "scope": "all-apis"},
                      auth=(cid, sec), what="Databricks' token endpoint")
        if resp.status_code != 200:
            raise r.failed(resp, "Databricks' token endpoint")
        doc = resp.json()
        return str(doc["access_token"]), int(doc.get("expires_in") or 3600)
    return {"Authorization": "Bearer " + TOKENS.get(digest("dbx", host, cid, sec), fetch)}


def _idem(ctx: Ctx) -> str:
    return digest("cairn-flow", ctx.run_id, ctx.step.get("name", ""), str(ctx.attempt))[:48]


def _dbx_state(run: dict) -> tuple[str, Optional[str], str, str]:
    """(life cycle, result, termination code, message) of a Jobs API 2.2 run or task: ``status`` (2.2) when there,
    else the older ``state``. The result is ``state.result_state`` when given, else what the termination code says
    (``SUCCESS``, ``CANCELED``, otherwise ``FAILED``)."""
    status = run.get("status") or {}
    state = run.get("state") or {}
    term = status.get("termination_details") or {}
    life = status.get("state") or state.get("life_cycle_state") or "PENDING"
    code = term.get("code") or ""
    result = state.get("result_state")
    if result is None and life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
        result = code if code in ("SUCCESS", "CANCELED") else "FAILED" if (code or life != "TERMINATED") else None
    msg = term.get("message") or state.get("state_message") or ""
    return life, result, code, msg


def run_databricks(b: Backends, ctx: Ctx) -> Any:
    s, st = ctx.step, dict(ctx.state)
    r = Remote(b, ctx)
    if not st.get("run_id"):
        st = {"platform": "databricks", "host": _dbx_host(s["host"]), "scope": s["secret_scope"], "auth": s["auth"],
              "token_key": s.get("token_key"), "client_id_key": s.get("client_id_key"),
              "client_secret_key": s.get("client_secret_key")}
        head = dbx_headers(r, st)
        if s.get("job_id") not in (None, ""):
            try:
                job_id = int(str(s["job_id"]).strip())
            except ValueError:
                raise StepFailed(f"job_id {s['job_id']!r} is not a Databricks job id (a number)")
            body: dict[str, Any] = {"job_id": job_id, "idempotency_token": _idem(ctx)}
            if s.get("job_parameters"):
                body["job_parameters"] = {str(k): v if isinstance(v, str) else json.dumps(v)
                                          for k, v in s["job_parameters"].items()}
            path, what = "/api/2.2/jobs/run-now", "run-now"
        else:
            body = {"run_name": f"cairn {ctx.flow}.{s['name']}", **(s.get("run") or {}), "idempotency_token": _idem(ctx)}
            path, what = "/api/2.2/jobs/runs/submit", "runs/submit"
        resp = r.call("POST", st["host"] + path, headers=head, json_body=body, what="Databricks")
        if resp.status_code != 200:
            raise r.failed(resp, f"Databricks {what}")
        st["run_id"] = int(resp.json()["run_id"])
        ctx.log(f"Databricks {what}: run {st['run_id']}")
        got = _dbx_get(r, st)
        st["run_page_url"] = got.get("run_page_url")
        if st["run_page_url"]:
            ctx.log(f"run page: {st['run_page_url']}")
        return _dbx_look(ctx, st, got)
    _overdue(b, ctx, "databricks")
    return _dbx_look(ctx, st, _dbx_get(r, st))


def _dbx_get(r: Remote, st: dict) -> dict:
    resp = r.call("GET", f"{st['host']}/api/2.2/jobs/runs/get?run_id={int(st['run_id'])}", headers=dbx_headers(r, st),
                  what="Databricks")
    if resp.status_code in (429, 502, 503, 504):
        return {"_retry": resp.status_code}
    if resp.status_code != 200:
        raise r.failed(resp, "Databricks runs/get")
    return resp.json()


def _dbx_look(ctx: Ctx, st: dict, run: dict) -> Any:
    if run.get("_retry"):
        return _later(ctx, st, f"Databricks answered {run['_retry']}; looking again")
    life, result, code, msg = _dbx_state(run)
    out = {"run_id": st["run_id"], "run_page_url": run.get("run_page_url") or st.get("run_page_url"),
           "job_id": run.get("job_id"), "life_cycle_state": life, "result_state": result,
           "duration_ms": run.get("run_duration") or run.get("execution_duration"),
           "tasks": [{"task_key": t.get("task_key"), "result_state": _dbx_state(t)[1]} for t in run.get("tasks") or []][:50]}
    if life in DBX_RUNNING:
        said = "" if st.get("said") == life else f"Databricks run {st['run_id']}: {life}"
        st["said"] = life
        return _later(ctx, st, said)
    if result in ("SUCCESS", "SUCCESS_WITH_FAILURES"):
        ctx.log(f"Databricks run {st['run_id']}: {result}")
        return Done(out)
    raise StepFailed(f"Databricks run {st['run_id']} ended {result or life}" + (f" ({code})" if code and code != result else "")
                     + (f": {msg[:400]}" if msg else "") + (f" ({out['run_page_url']})" if out["run_page_url"] else ""))


def cancel_databricks(r: Remote, st: dict) -> None:
    resp = r.call("POST", f"{st['host']}/api/2.2/jobs/runs/cancel", headers=dbx_headers(r, st),
                  json_body={"run_id": int(st["run_id"])}, what="Databricks")
    if resp.status_code != 200:
        raise r.failed(resp, "Databricks runs/cancel")


# ---- Snowflake (SQL API v2, key-pair JWT) ----------------------------------------------------------------------
IDENT = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]{1,255}")(?:\.(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]{1,255}")){0,2}$')
ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")


def snowflake_jwt(account: str, user: str, key: Any) -> str:
    """The key-pair JWT Snowflake's SQL API takes: ``iss`` ``ACCOUNT.USER.SHA256:<fingerprint of the public key>``,
    ``sub`` ``ACCOUNT.USER``, an hour at most. The account is upper case, without a region (``xy12345.us-east-1`` is
    ``XY12345``)."""
    from cryptography.hazmat.primitives import serialization

    acct = account.split(".")[0].upper()
    usr = user.upper()
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    now = int(time.time())
    return sign_rs256(key, {"iss": f"{acct}.{usr}.{fp}", "sub": f"{acct}.{usr}", "iat": now, "exp": now + 3540})


def snowflake_host(account: str, host: Optional[str]) -> str:
    if host:
        h = host.strip().rstrip("/")
        return h if "://" in h else "https://" + h
    return f"https://{account.lower()}.snowflakecomputing.com"


def sf_headers(r: Remote, st: dict) -> dict:
    pem = r.secret(st["scope"], st.get("private_key_key") or "private_key", "the Snowflake private key")
    phrase = r.secret(st["scope"], st["passphrase_key"], "the private key's passphrase") if st.get("passphrase_key") else None
    key = load_private_key(pem, phrase, "the Snowflake private key")
    return {"Authorization": "Bearer " + snowflake_jwt(st["account"], st["user"], key),
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT", "Accept": "application/json",
            "Content-Type": "application/json"}


def snowflake_statement(s: dict) -> str:
    if s.get("task"):
        if not IDENT.match(str(s["task"]).strip()):
            raise StepFailed(f"task {s['task']!r} is a task name (db.schema.task)")
        return f"EXECUTE TASK {str(s['task']).strip()}"
    return str(s["statement"])


def run_snowflake(b: Backends, ctx: Ctx) -> Any:
    s, st = ctx.step, dict(ctx.state)
    r = Remote(b, ctx)
    if not st.get("handle"):
        if not ACCOUNT.match(str(s["account"])):
            raise StepFailed(f"account {s['account']!r} is a Snowflake account identifier (orgname-account or a locator)")
        st = {"platform": "snowflake", "host": snowflake_host(s["account"], s.get("host")), "scope": s["secret_scope"],
              "account": s["account"], "user": s["user"], "private_key_key": s.get("private_key_key"),
              "passphrase_key": s.get("passphrase_key")}
        body: dict[str, Any] = {"statement": snowflake_statement(s), "timeout": int(s.get("timeout_s") or 3600)}
        for f in ("database", "schema", "warehouse", "role"):
            if s.get(f):
                body[f] = str(s[f])
        if s.get("multi_statement"):
            body["parameters"] = {"MULTI_STATEMENT_COUNT": "0"}
        request_id = str(uuid.UUID(digest("cairn-flow-sf", ctx.run_id, s["name"], str(ctx.attempt))[:32]))
        url = f"{st['host']}/api/v2/statements?" + urlencode({"requestId": request_id, "async": "true"})
        resp = r.call("POST", url, headers=sf_headers(r, st), json_body=body, what="Snowflake")
        doc = body_of(resp) or {}
        if resp.status_code not in (200, 202) or not isinstance(doc, dict) or not doc.get("statementHandle"):
            raise _sf_failed(r, resp, doc)
        st["handle"] = doc["statementHandle"]
        ctx.log(f"Snowflake statement {st['handle']} {'started' if resp.status_code == 202 else 'done'}")
        if resp.status_code == 200:
            return Done(_sf_output(st, doc))
        return _later(ctx, st)
    _overdue(b, ctx, "snowflake")
    resp = r.call("GET", f"{st['host']}/api/v2/statements/{quote(st['handle'], safe='')}", headers=sf_headers(r, st),
                  what="Snowflake")
    doc = body_of(resp) or {}
    if resp.status_code == 202 or resp.status_code in (429, 503, 504):
        return _later(ctx, st)
    if resp.status_code == 200:
        out = _sf_output(st, doc)
        ctx.log(f"Snowflake statement {st['handle']}: {out['rows']} rows")
        return Done(out)
    raise _sf_failed(r, resp, doc)


def _sf_failed(r: Remote, resp: Any, doc: Any) -> StepFailed:
    if isinstance(doc, dict) and doc.get("message"):
        code = doc.get("code") or ""
        state = doc.get("sqlState") or ""
        return StepFailed(f"Snowflake answered {resp.status_code} ({code}{', SQL state ' + state if state else ''}): "
                          f"{r.scrub(doc['message'])[:400]}")
    return r.failed(resp, "Snowflake")


def _sf_output(st: dict, doc: dict) -> dict:
    meta = doc.get("resultSetMetaData") or {}
    cols = [c.get("name") for c in meta.get("rowType") or []]
    data = doc.get("data") or []
    out = {"statement_handle": doc.get("statementHandle") or st["handle"], "rows": int(meta.get("numRows") or len(data)),
           "columns": cols, "preview": data[:20], "message": doc.get("message") or ""}
    if doc.get("statementHandles"):
        out["statement_handles"] = doc["statementHandles"]
    return out


def cancel_snowflake(r: Remote, st: dict) -> None:
    resp = r.call("POST", f"{st['host']}/api/v2/statements/{quote(st['handle'], safe='')}/cancel",
                  headers=sf_headers(r, st), json_body={}, what="Snowflake")
    if resp.status_code not in (200, 202):
        raise _sf_failed(r, resp, body_of(resp))


# ---- BigQuery (jobs.insert, jobs.get) --------------------------------------------------------------------------
BQ_SCOPE = "https://www.googleapis.com/auth/bigquery"
BQ_TABLE = re.compile(r"^([A-Za-z0-9-]{1,63}(?:[.:][A-Za-z0-9-]{1,63})?)\.([A-Za-z0-9_]{1,1024})\.([A-Za-z0-9_$-]{1,1024})$")


def bq_headers(r: Remote, st: dict) -> dict:
    """A service account's access token: a JWT it signs for the BigQuery scope, exchanged at its ``token_uri``."""
    raw = r.secret(st["scope"], st.get("key_key") or "service_account_json", "the BigQuery service account key")
    try:
        sa = json.loads(raw)
        email, pem = sa["client_email"], sa["private_key"]
        token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    except (ValueError, KeyError, TypeError):
        raise StepFailed("the BigQuery service account key is not a service account's JSON key")
    key = load_private_key(pem, None, "the service account's private key")

    def fetch() -> tuple[str, int]:
        now = int(time.time())
        assertion = sign_rs256(key, {"iss": email, "scope": BQ_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
                               {"kid": sa["private_key_id"]} if sa.get("private_key_id") else None)
        resp = r.call("POST", token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                               "assertion": assertion}, what="the key's token_uri")
        if resp.status_code != 200:
            raise r.failed(resp, "Google's token endpoint")
        doc = resp.json()
        return str(doc["access_token"]), int(doc.get("expires_in") or 3600)
    return {"Authorization": "Bearer " + TOKENS.get(digest("bq", token_uri, email, pem), fetch)}


def bq_job_id(ctx: Ctx) -> str:
    raw = f"cairn_{ctx.flow}_{ctx.step.get('name', '')}_{ctx.run_id[:12]}_{ctx.attempt}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:1024]


def _bq_base(st: dict) -> str:
    return f"{st['api']}/bigquery/v2/projects/{quote(st['project'], safe='')}"


def _bq_loc(st: dict) -> str:
    return f"?location={quote(st['location'], safe='')}" if st.get("location") else ""


def _label(v: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", v.lower())[:63]


def run_bigquery(b: Backends, ctx: Ctx) -> Any:
    s, st = ctx.step, dict(ctx.state)
    r = Remote(b, ctx)
    if not st.get("job_id"):
        st = {"platform": "bigquery", "api": str(s.get("api_url") or "https://bigquery.googleapis.com").rstrip("/"),
              "scope": s["secret_scope"], "key_key": s.get("key_key"), "project": str(s["project"]),
              "location": str(s.get("location") or ""), "job_id": bq_job_id(ctx)}
        query: dict[str, Any] = {"query": s["query"], "useLegacySql": False}
        if s.get("destination_table"):
            m = BQ_TABLE.match(str(s["destination_table"]))
            if not m:
                raise StepFailed(f"destination_table {s['destination_table']!r} is project.dataset.table")
            query["destinationTable"] = {"projectId": m.group(1), "datasetId": m.group(2), "tableId": m.group(3)}
            query["writeDisposition"] = s.get("write_disposition") or "WRITE_EMPTY"
        if s.get("max_bytes_billed"):
            query["maximumBytesBilled"] = str(int(s["max_bytes_billed"]))
        ref = {"projectId": st["project"], "jobId": st["job_id"], **({"location": st["location"]} if st["location"] else {})}
        labels = {"cairn_flow": _label(ctx.flow), "cairn_step": _label(s["name"]),
                  **{_label(str(k)): _label(str(v)) for k, v in (s.get("labels") or {}).items()}}
        body = {"jobReference": ref, "configuration": {"query": query, "labels": labels}}
        resp = r.call("POST", f"{_bq_base(st)}/jobs", headers=bq_headers(r, st), json_body=body,
                      credentials=True, what="BigQuery")
        if resp.status_code == 409:
            ctx.log(f"BigQuery job {st['job_id']} was already inserted; following it")
        elif resp.status_code not in (200, 201):
            raise _bq_failed(r, resp)
        else:
            ctx.log(f"BigQuery job {st['job_id']} inserted")
        st["console_url"] = ("https://console.cloud.google.com/bigquery?project=" + quote(st["project"], safe="")
                             + "&j=" + quote(f"bq:{st['location'] or 'US'}:{st['job_id']}", safe="") + "&page=queryresults")
        return _bq_look(r, ctx, st)
    _overdue(b, ctx, "bigquery")
    return _bq_look(r, ctx, st)


def _bq_failed(r: Remote, resp: Any) -> StepFailed:
    doc = body_of(resp)
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        e = doc["error"]
        return StepFailed(f"BigQuery answered {resp.status_code} ({e.get('status') or e.get('code')}): "
                          f"{r.scrub(e.get('message') or '')[:400]}")
    return r.failed(resp, "BigQuery")


def _bq_look(r: Remote, ctx: Ctx, st: dict) -> Any:
    head = bq_headers(r, st)
    resp = r.call("GET", f"{_bq_base(st)}/jobs/{quote(st['job_id'], safe='')}{_bq_loc(st)}", headers=head, what="BigQuery")
    if resp.status_code in (429, 500, 502, 503, 504):
        return _later(ctx, st, f"BigQuery answered {resp.status_code}; looking again")
    if resp.status_code != 200:
        raise _bq_failed(r, resp)
    job = resp.json()
    status = job.get("status") or {}
    if status.get("state") != "DONE":
        return _later(ctx, st)
    if status.get("errorResult"):
        e = status["errorResult"]
        raise StepFailed(f"BigQuery job {st['job_id']} failed ({e.get('reason')}): {r.scrub(e.get('message') or '')[:400]}")
    stats = (job.get("statistics") or {}).get("query") or {}
    out: dict[str, Any] = {"job_id": st["job_id"], "project": st["project"],
                           "location": (job.get("jobReference") or {}).get("location") or st.get("location") or None,
                           "bytes_processed": int(stats.get("totalBytesProcessed") or 0),
                           "console_url": st.get("console_url")}
    res = r.call("GET", f"{_bq_base(st)}/queries/{quote(st['job_id'], safe='')}"
                 + (_bq_loc(st) + "&" if st.get("location") else "?") + "maxResults=20", headers=head, what="BigQuery")
    if res.status_code == 200:
        doc = res.json()
        fields = [f.get("name") for f in (doc.get("schema") or {}).get("fields") or []]
        rows = [[c.get("v") for c in (row.get("f") or [])] for row in doc.get("rows") or []]
        out.update(rows=int(doc.get("totalRows") or len(rows)), columns=fields, preview=rows[:20])
    elif res.status_code in (429, 500, 502, 503, 504) and st.get("result_tries", 0) < 5:
        st["result_tries"] = st.get("result_tries", 0) + 1
        return _later(ctx, st, f"BigQuery answered {res.status_code} for the job's results; looking again")
    else:
        raise _bq_failed(r, res)           # the results cannot be read (or the query was refused): never "0 rows"
    ctx.log(f"BigQuery job {st['job_id']} done: {out.get('rows', 0)} rows, {out['bytes_processed']} bytes processed")
    return Done(out)


def cancel_bigquery(r: Remote, st: dict) -> None:
    resp = r.call("POST", f"{_bq_base(st)}/jobs/{quote(st['job_id'], safe='')}/cancel{_bq_loc(st)}",
                  headers=bq_headers(r, st), json_body={}, what="BigQuery")
    if resp.status_code not in (200, 404):
        raise _bq_failed(r, resp)


# ---- cancelling ------------------------------------------------------------------------------------------------
CANCELLERS: dict[str, Callable[[Remote, dict], None]] = {
    "databricks": cancel_databricks, "snowflake": cancel_snowflake, "bigquery": cancel_bigquery,
}
HANDLES = {"databricks": "run_id", "snowflake": "handle", "bigquery": "job_id"}


def cancel(b: Backends, kind: str, owner: str, state: Optional[dict], run_id: str = "", flow: str = "",
           step: str = "") -> bool:
    """Cancels a step's remote work from its state (as the owner); True when it was asked. Never raises: a cancel
    that does not get through is logged (the run is cancelled in Cairn either way)."""
    fn = CANCELLERS.get(kind)
    if fn is None or not state or not state.get(HANDLES[kind]):
        return False
    ctx = Ctx(owner=owner, flow=flow, run_id=run_id, step={"name": step, "timeout_s": 600}, attempt=0, values={},
              started_ms=_now_ms(), deadline_ms=_now_ms() + 600_000, state=dict(state))
    try:
        fn(Remote(b, ctx), state)
        logger.info("flow %s run %s: cancelled the %s work %s of %s", flow, run_id, kind, state.get(HANDLES[kind]), step)
        return True
    except Exception as e:  # noqa: BLE001 - logged; the step is cancelled in Cairn regardless
        logger.warning("flow %s run %s: could not cancel the %s work of %s: %s", flow, run_id, kind, step, e)
        return False


EXECUTORS: dict[str, Callable[[Backends, Ctx], Any]] = {
    "http": run_http, "databricks": run_databricks, "snowflake": run_snowflake, "bigquery": run_bigquery,
}


def _register() -> None:
    """These steps run like the others (cairn.flows.steps.EXECUTORS) and wait without holding a worker."""
    from cairn.flows import steps

    steps.EXECUTORS.update(EXECUTORS)
    steps.WAITING = tuple(dict.fromkeys((*steps.WAITING, *EXECUTORS)))


_register()
