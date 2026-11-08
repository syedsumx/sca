"""Code sandboxes: Python (or SQL over DuckDB) that agents and people run, each run in a container of its own.

``POST /v1/sandboxes/runs`` with ``{"language": "python" | "sql", "code": "...", "files": [{"name", "content"}],
"network": {"cairn": false, "allow": ["host[:port]"]}, "limits": {"timeout_s", "memory_mb", "cpus"}}`` runs the code
and answers when it has finished: ``status`` (``succeeded``, ``failed``, ``timeout``, ``memory``), ``result`` (the
value the code left in ``result``; for SQL the rows), ``stdout``, ``stderr`` and ``files`` (what the code wrote under
``out/``, base64). The same is offered to agents: the ``run_python`` tool (custom agents, the assistant, MCP) and
``ctx.run_python`` in agent apps (``cairn.agentkit``).

* **Isolated, every time.** Never in Cairn's process: ``CAIRN_SANDBOX_RUNTIME=docker`` starts a new container per run
  and ``kubernetes`` a pod per run (in ``CAIRN_SANDBOX_K8S_NAMESPACE``, optionally under a sandboxing runtime class
  such as gVisor, ``CAIRN_SANDBOX_K8S_RUNTIME_CLASS``). The container runs as a non-root user with no capabilities,
  no new privileges, a read-only root file system, a process limit and the run's CPU, memory and time limits. The
  code, the runner and the input files are mounted read-only; the working directory (``/work``, where the inputs
  are copied) is an in-memory file system that disappears with the run. Without a runtime, runs are refused (503).
* **No network by default.** The container has no network interface at all (docker ``--network none``; on
  Kubernetes a NetworkPolicy that denies all traffic). A run that asks for network gets exactly one way out: Cairn's
  egress proxy for that run (``HTTP_PROXY``/``HTTPS_PROXY`` in the container), which lets through only
  ``network.allow`` hosts (each one the operator lists in ``CAIRN_SANDBOX_ALLOWED_HOSTS``; others are refused before
  the run starts) and, with ``network.cairn``, Cairn itself at ``http://cairn``. Names resolve on Cairn's side and
  private, loopback and link-local addresses are refused (``CAIRN_SANDBOX_ALLOW_PRIVATE_EGRESS=true`` lifts that for
  allowed hosts on a private network). Traffic is counted and capped (``CAIRN_SANDBOX_MAX_EGRESS_MB``, 100).
* **Data as the caller, governed.** With ``network.cairn`` the run gets ``CAIRN_URL`` and ``CAIRN_TOKEN``: a token
  for the person who asked (through the sandbox's agent identity, ``agt_cairn_sandbox``), narrowed to ``read`` and
  ``query_data`` (and, when an agent asked, to that agent's scopes and intent), valid for the run and revoked when
  it ends. What the code reads through Cairn is what that person may read, masked and filtered as for them. The
  token reaches the container through an env file (docker) or a Secret (Kubernetes), never a command line.
* **Who.** Running code needs ``RUN_CODE``. Each person has at most ``CAIRN_SANDBOX_MAX_PER_PERSON`` (2) runs at a
  time and Cairn ``CAIRN_SANDBOX_MAX_RUNNING`` (8); more are refused (429). Limits are capped by
  ``CAIRN_SANDBOX_MAX_TIMEOUT_S`` (600), ``CAIRN_SANDBOX_MAX_MEMORY_MB`` (2048) and ``CAIRN_SANDBOX_MAX_CPUS`` (2).
* **Metered and audited.** Every run is recorded (who, which agent, language, limits, network, status, duration,
  egress and output sizes; never the code or its output) and audited (``sandbox.run_<status>``);
  ``GET /v1/sandboxes/usage`` sums them per person and day (runs, CPU seconds, memory and egress). Records are kept
  ``CAIRN_SANDBOX_KEEP_DAYS`` (30) days.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from sqlalchemy import BigInteger, Column, Float, Integer, String, Table, Text, and_, delete, func, insert, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

PRIVILEGE = "RUN_CODE"
AGENT_ID = "agt_cairn_sandbox"
DATA_SCOPES = ("read", "query_data")
IMAGE = "cairn/sandbox:latest"
LANGUAGES = ("python", "sql")
FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,127}){0,4}$")
HOST = re.compile(r"^(?=.{1,253}(:\d{1,5})?$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
                  r"(:\d{1,5})?$")
PROXY_PORT = 3128          # where the runner's bridge listens inside a docker sandbox
SOCKET_DIR = "/run/cairn"  # where the egress socket is mounted inside a docker sandbox
CAIRN_HOST = "cairn"       # the name Cairn has inside a sandbox (the proxy knows where it really is)

sandbox_runs = Table(
    "cairn_sandbox_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("agent", String(64), nullable=True),               # the agent that asked, if one did
    Column("language", String(10), nullable=False),
    Column("status", String(12), nullable=False),             # running | succeeded | failed | timeout | memory | lost
    Column("network", JSONText, nullable=False),              # {"cairn": bool, "allow": [...]}
    Column("timeout_s", Integer, nullable=False),
    Column("memory_mb", Integer, nullable=False),
    Column("cpus", Float, nullable=False),
    Column("started_ms", BigInteger, nullable=False, index=True),
    Column("finished_ms", BigInteger, nullable=True),
    Column("duration_ms", BigInteger, nullable=True),
    Column("exit_code", Integer, nullable=True),
    Column("input_bytes", BigInteger, nullable=False),
    Column("output_bytes", BigInteger, nullable=True),
    Column("egress_bytes", BigInteger, nullable=True),
    Column("files", JSONText, nullable=True),                 # [{"name", "bytes"}] of what the run returned
    Column("error", Text, nullable=True),
)


class SandboxError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- what runs inside the sandbox
# The runner (the container's first process): copies the inputs into the working directory, starts the bridge to
# the egress proxy when the run has network, runs the code in a child process under the time limit, then prints one
# line after a per-run marker with what the run produced. Standard library only: any Python 3.9+ image works.
RUNNER = r'''
import base64, json, os, socket, subprocess, sys, threading, time

MARK = os.environ.pop("CAIRN_SANDBOX_MARK", "")
LIMIT = float(os.environ.pop("CAIRN_SANDBOX_TIMEOUT", "60"))
MAX_OUT = int(os.environ.pop("CAIRN_SANDBOX_MAX_OUTPUT", str(5 << 20)))
MAX_TEXT = int(os.environ.pop("CAIRN_SANDBOX_MAX_TEXT", str(256 << 10)))
SOCK = "/run/cairn/egress.sock"


def splice(a, b):
    def one(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d:
                    break
                y.sendall(d)
        except OSError:
            pass
        try:
            y.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    ts = [threading.Thread(target=one, args=(a, b), daemon=True), threading.Thread(target=one, args=(b, a), daemon=True)]
    for t in ts:
        t.start()

    def close():
        for t in ts:
            t.join()
        a.close()
        b.close()
    threading.Thread(target=close, daemon=True).start()


def bridge(srv):
    while True:
        c, _ = srv.accept()
        u = socket.socket(socket.AF_UNIX)
        try:
            u.connect(SOCK)
        except OSError:
            c.close()
            continue
        splice(c, u)


if os.path.exists(SOCK):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 3128))
    srv.listen(64)
    threading.Thread(target=bridge, args=(srv,), daemon=True).start()

for base, _, names in os.walk("/sandbox/inputs"):
    for n in names:
        src = os.path.join(base, n)
        dst = os.path.join("/work", os.path.relpath(src, "/sandbox/inputs"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as i, open(dst, "wb") as o:
            o.write(i.read())
os.makedirs("/work/out", exist_ok=True)
env = dict(os.environ, HOME="/work", PYTHONDONTWRITEBYTECODE="1", MPLCONFIGDIR="/tmp")
status, rc, started = "ok", None, time.monotonic()
with open("/tmp/.stdout", "wb") as so, open("/tmp/.stderr", "wb") as se:
    p = subprocess.Popen([sys.executable, "-u", "/sandbox/wrap.py"], cwd="/work", stdout=so, stderr=se, env=env)
    try:
        rc = p.wait(timeout=LIMIT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        status = "timeout"
if status == "ok" and rc is not None and rc < 0 and -rc == 9:
    status = "killed"                      # the kernel killed it: almost always the memory limit


def text(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_TEXT + 1)
    except OSError:
        return "", False
    return data[:MAX_TEXT].decode("utf-8", "replace"), len(data) > MAX_TEXT


out, out_cut = text("/tmp/.stdout")
err, err_cut = text("/tmp/.stderr")
result = None
if status == "ok" and rc == 0:
    try:
        with open("/tmp/.result.json") as fh:
            result = json.loads(fh.read(MAX_OUT))
    except (OSError, ValueError):
        result = None
files, left, skipped = [], MAX_OUT, []
for base, _, names in sorted(os.walk("/work/out")):
    for n in sorted(names):
        path = os.path.join(base, n)
        rel = os.path.relpath(path, "/work/out")
        try:
            size = os.path.getsize(path)
            if os.path.islink(path) or size > left:
                skipped.append(rel)
                continue
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            skipped.append(rel)
            continue
        left -= len(data)
        files.append({"name": rel, "bytes": len(data), "content": base64.b64encode(data).decode()})
envelope = {"status": status, "exit_code": rc, "stdout": out, "stderr": err, "truncated": out_cut or err_cut,
            "result": result, "files": files, "skipped_files": skipped,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}
sys.stdout.write("\n" + MARK + json.dumps(envelope) + "\n")
sys.stdout.flush()
'''

# The child: runs the person's code (or their SQL over DuckDB, on the files in the working directory) and keeps the
# value left in ``result``.
WRAP = r'''
import json, os, sys, traceback

lang = os.environ.pop("CAIRN_SANDBOX_LANGUAGE", "python")
ns = {"__name__": "__main__", "__file__": "main.py"}
try:
    if lang == "sql":
        try:
            import duckdb
        except ImportError:
            sys.exit("this sandbox image has no DuckDB: SQL runs need the cairn/sandbox image")
        con = duckdb.connect(":memory:")
        cur = con.execute(open("/sandbox/main.sql").read())
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(10000)
        ns["result"] = {"columns": cols, "rows": [list(r) for r in rows]}
    else:
        sys.path.insert(0, "/work")
        exec(compile(open("/sandbox/main.py").read(), "main.py", "exec"), ns)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(1)
if "result" in ns:
    with open("/tmp/.result.json", "w") as fh:
        json.dump(ns["result"], fh, default=str)
'''


def split_output(text: str, mark: str) -> tuple[str, Optional[dict]]:
    """(what came before the marker, the run's envelope or None)."""
    if not mark or mark not in text:
        return text, None
    before, _, after = text.rpartition(mark)
    try:
        return before, json.loads(after.strip().splitlines()[0])
    except (ValueError, IndexError):
        return before, None


# --------------------------------------------------------------------------- the egress proxy
class Policy:
    """Where one run may go: ``allow`` (host or host:port; port 443 for CONNECT and 80 for plain HTTP when none is
    given) and, when ``cairn`` is Cairn's URL, Cairn itself as ``http://cairn``."""

    def __init__(self, allow: list[str], cairn: str = "", allow_private: bool = False, max_bytes: int = 100 << 20,
                 cairn_ca: str = ""):
        self.allow = {a.lower() for a in allow}
        self.cairn = urlsplit(cairn) if cairn else None
        self.allow_private, self.max_bytes, self.cairn_ca = allow_private, max_bytes, cairn_ca
        self.used = 0
        self.lock = threading.Lock()

    def allows(self, host: str, port: int, default_port: int) -> bool:
        host = host.lower().rstrip(".")
        return f"{host}:{port}" in self.allow or (port == default_port and host in self.allow)

    def count(self, n: int) -> bool:
        with self.lock:
            self.used += n
            return self.used <= self.max_bytes


def _deny(c: socket.socket, code: int, text: str) -> None:
    body = text.encode()
    reason = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 502: "Bad Gateway"}.get(code, "Error")
    try:
        c.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                  f"Connection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _splice(a: socket.socket, b: socket.socket, policy: Policy) -> None:
    def one(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d or not policy.count(len(d)):
                    break
                y.sendall(d)
        except OSError:
            pass
        try:
            y.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    ts = [threading.Thread(target=one, args=(a, b), daemon=True), threading.Thread(target=one, args=(b, a), daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


def _connect_public(host: str, port: int, allow_private: bool) -> socket.socket:
    """A connection to ``host``, resolved here; refused when any address it resolves to is private (unless allowed),
    and made to the address checked (no second lookup to rebind)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"{host} does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                  or ip.is_multicast or ip.is_unspecified):
            raise PermissionError(f"{host} resolves to a private address")
    family, kind, proto, _, addr = infos[0]
    s = socket.socket(family, kind, proto)
    s.settimeout(30)
    s.connect(addr)
    s.settimeout(None)
    return s


def handle_proxy_connection(c: socket.socket, policy_for: Callable[[dict], Optional[Policy]]) -> None:
    """One client connection to the egress proxy: CONNECT to an allowed host, or a plain HTTP request to an allowed
    host or to Cairn (``http://cairn/...``)."""
    try:
        c.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(65536)
            if not d:
                c.close()
                return
            buf += d
            if len(buf) > 65536:
                _deny(c, 400, "request head too large")
                c.close()
                return
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            _deny(c, 400, "not an HTTP request")
            c.close()
            return
        headers: list[tuple[str, str]] = []
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers.append((k.strip(), v.strip()))
        policy = policy_for({k.lower(): v for k, v in headers})
        if policy is None:
            _deny(c, 407, "this proxy is only for Cairn's sandboxes")
            c.close()
            return
        c.settimeout(None)
        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]").lower()
            if not host or not port_s.isdigit() or not policy.allows(host, int(port_s), 443):
                _deny(c, 403, f"egress to {target} is not allowed from this sandbox")
                c.close()
                return
            try:
                up = _connect_public(host, int(port_s), policy.allow_private)
            except PermissionError as e:
                _deny(c, 403, str(e))
                c.close()
                return
            except OSError as e:
                _deny(c, 502, f"{target}: {e}")
                c.close()
                return
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
            _splice(c, up, policy)
            return
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            _deny(c, 400, "send absolute http:// URLs, or CONNECT for https")
            c.close()
            return
        host, port = u.hostname.lower(), u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if host == CAIRN_HOST and policy.cairn is not None:
            cu = policy.cairn
            up = socket.create_connection((cu.hostname, cu.port or (443 if cu.scheme == "https" else 80)), 30)
            up.settimeout(None)
            if cu.scheme == "https":
                ctx = ssl.create_default_context(cafile=policy.cairn_ca or None)
                up = ctx.wrap_socket(up, server_hostname=cu.hostname)
            path, host_header = cu.path.rstrip("/") + path, cu.netloc
        elif host != CAIRN_HOST and policy.allows(host, port, 80):
            try:
                up = _connect_public(host, port, policy.allow_private)
            except PermissionError as e:
                _deny(c, 403, str(e))
                c.close()
                return
            except OSError as e:
                _deny(c, 502, f"{host}: {e}")
                c.close()
                return
            host_header = u.netloc
        else:
            _deny(c, 403, f"egress to {host}:{port} is not allowed from this sandbox")
            c.close()
            return
        keep = [(k, v) for k, v in headers if k.lower() not in ("host", "connection", "keep-alive", "proxy-connection",
                                                                   "proxy-authorization", "te", "upgrade")]
        out = f"{method} {path} {version}\r\nHost: {host_header}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in keep) + \
            "Connection: close\r\n\r\n"
        up.sendall(out.encode("latin-1") + rest)
        _splice(c, up, policy)
    except Exception as e:  # noqa: BLE001 - one connection's failure, never the proxy's
        logger.debug("sandbox egress connection failed: %s", e)
        try:
            c.close()
        except OSError:
            pass


class SocketProxy:
    """One run's egress proxy, on a Unix socket mounted into its container (docker): the only way out."""

    def __init__(self, path: str, policy: Policy):
        self.path, self.policy = path, policy
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        os.chmod(path, 0o777)                     # the sandbox's user connects; the directory is the run's alone
        self.srv.listen(64)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True, name="cairn-sandbox-egress").start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=handle_proxy_connection, args=(c, lambda _h: self.policy), daemon=True).start()

    def close(self) -> None:
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass


class ProxyCredentials:
    """Signed, self-contained proxy credentials (Kubernetes sandboxes reach Cairn's egress proxy over TCP, and any
    replica may answer): ``base64(policy).hmac`` in ``Proxy-Authorization: Basic run:<credential>``."""

    def __init__(self, key: bytes):
        if len(key) < 16:
            raise SandboxError("the sandbox egress proxy needs a key (CAIRN_SANDBOX_PROXY_KEY, at least 16 bytes)", 503)
        self.key = key

    def issue(self, allow: list[str], cairn: bool, ttl_s: int) -> str:
        body = base64.urlsafe_b64encode(json.dumps({"a": allow, "c": cairn, "e": int(time.time()) + ttl_s,
                                                    "n": secrets.token_hex(8)}).encode()).decode().rstrip("=")
        return body + "." + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def check(self, header: str) -> Optional[dict]:
        if not header.lower().startswith("basic "):
            return None
        try:
            user, _, cred = base64.b64decode(header[6:].strip()).decode().partition(":")
            body, _, sig = cred.partition(".")
            if user != "run" or not hmac.compare_digest(sig, hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()):
                return None
            doc = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, UnicodeDecodeError):
            return None
        return doc if int(doc.get("e") or 0) >= time.time() else None


class TcpProxy:
    """Cairn's egress proxy for Kubernetes sandboxes (``CAIRN_SANDBOX_K8S_PROXY_LISTEN``): each request carries its
    run's signed credentials, which say where it may go."""

    def __init__(self, listen: str, creds: ProxyCredentials, cairn_url: str, allow_private: bool, max_bytes: int,
                 cairn_ca: str = ""):
        host, _, port = listen.rpartition(":")
        self.creds, self.cairn_url, self.allow_private, self.max_bytes = creds, cairn_url, allow_private, max_bytes
        self.cairn_ca = cairn_ca
        self.srv = socket.create_server((host or "0.0.0.0", int(port)), reuse_port=False)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True, name="cairn-sandbox-egress-tcp").start()

    def _policy(self, headers: dict) -> Optional[Policy]:
        doc = self.creds.check(headers.get("proxy-authorization", ""))
        if doc is None:
            return None
        return Policy(list(doc.get("a") or []), self.cairn_url if doc.get("c") else "", self.allow_private,
                      self.max_bytes, self.cairn_ca)

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=handle_proxy_connection, args=(c, self._policy), daemon=True).start()

    def close(self) -> None:
        self.srv.close()


# --------------------------------------------------------------------------- runtimes
class Spec:
    """One run, as the runtime needs it."""

    def __init__(self, run_id: str, language: str, code: str, files: dict[str, bytes], env: dict[str, str],
                 timeout_s: int, memory_mb: int, cpus: float, network: dict, max_output: int):
        self.run_id, self.language, self.code, self.files, self.env = run_id, language, code, files, env
        self.timeout_s, self.memory_mb, self.cpus, self.network, self.max_output = (timeout_s, memory_mb, cpus,
                                                                                     network, max_output)
        self.mark = "=====CAIRN-SANDBOX-" + secrets.token_hex(12) + "====="

    def runner_env(self) -> dict[str, str]:
        return {**self.env, "CAIRN_SANDBOX_MARK": self.mark, "CAIRN_SANDBOX_TIMEOUT": str(self.timeout_s),
                "CAIRN_SANDBOX_MAX_OUTPUT": str(self.max_output), "CAIRN_SANDBOX_LANGUAGE": self.language}

    def program(self) -> dict[str, str]:
        return {"runner.py": RUNNER, "wrap.py": WRAP, ("main.sql" if self.language == "sql" else "main.py"): self.code}


def _finish(raw_rc: int, text: str, spec: Spec, oom: bool, timed_out: bool) -> dict:
    log, env = split_output(text, spec.mark)
    if env is None:
        status = "memory" if oom else ("timeout" if timed_out else "failed")
        why = {"memory": f"the run went over its memory limit ({spec.memory_mb} MB) and was stopped",
               "timeout": f"the run went over its time limit ({spec.timeout_s} s) and was stopped"}.get(
            status, f"the sandbox exited with status {raw_rc}: {log.strip()[-500:]}")
        return {"status": status, "exit_code": raw_rc, "stdout": "", "stderr": "", "result": None, "files": [],
                "error": why}
    st = env.get("status")
    if st == "timeout" or timed_out:
        status, error = "timeout", f"the run went over its time limit ({spec.timeout_s} s) and was stopped"
    elif st == "killed" or oom:
        status, error = "memory", f"the run went over its memory limit ({spec.memory_mb} MB) and was stopped"
    elif env.get("exit_code") == 0:
        status, error = "succeeded", None
    else:
        status = "failed"
        tail = (env.get("stderr") or "").strip().splitlines()
        error = f"the code exited with status {env.get('exit_code')}" + (f": {tail[-1][:500]}" if tail else "")
    return {"status": status, "exit_code": env.get("exit_code"), "stdout": env.get("stdout") or "",
            "stderr": env.get("stderr") or "", "result": env.get("result"), "files": env.get("files") or [],
            "skipped_files": env.get("skipped_files") or [], "truncated": bool(env.get("truncated")), "error": error}


class DockerSandbox:
    """A new container per run, with no network interface; network, when asked for, only through the run's proxy."""

    def __init__(self, image: str = IMAGE, docker: str = "docker", work_mb: int = 256, pids: int = 128,
                 runtime: str = ""):
        self.image, self.docker, self.work_mb, self.pids, self.runtime = image, docker, work_mb, pids, runtime

    def run(self, spec: Spec, policy: Optional[Policy]) -> dict:
        root = tempfile.mkdtemp(prefix=f"cairn-sbx-{spec.run_id[:12]}-")
        code, secret_dir, sock_dir = (os.path.join(root, d) for d in ("code", "env", "sock"))
        proxy = None
        name = f"cairn-sbx-{spec.run_id}"
        try:
            os.chmod(root, 0o711)
            os.makedirs(os.path.join(code, "inputs"))
            os.makedirs(secret_dir, mode=0o700)
            for fname, content in spec.program().items():
                with open(os.path.join(code, fname), "w") as fh:
                    fh.write(content)
            for fname, data in spec.files.items():
                path = os.path.join(code, "inputs", fname)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            for base, _dirs, names in os.walk(code):
                os.chmod(base, 0o755)
                for n in names:
                    os.chmod(os.path.join(base, n), 0o644)
            env_file = os.path.join(secret_dir, "env")
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:            # the token through a file docker reads, never the command line
                for k, v in spec.runner_env().items():
                    if "\n" in v or "\r" in v:
                        raise SandboxError(f"{k}: a value on one line")
                    fh.write(f"{k}={v}\n")
            mounts = ["-v", f"{code}:/sandbox:ro"]
            if policy is not None:
                os.makedirs(sock_dir)
                os.chmod(sock_dir, 0o711)
                proxy = SocketProxy(os.path.join(sock_dir, "egress.sock"), policy)
                mounts += ["-v", f"{sock_dir}:{SOCKET_DIR}:ro"]
            cmd = [self.docker, "run", "--name", name, "--env-file", env_file, "--network", "none",
                   "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", f"{spec.memory_mb}m", "--memory-swap", f"{spec.memory_mb}m", "--cpus", str(spec.cpus),
                   "--pids-limit", str(self.pids), "--read-only", "--ipc", "none",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                   "--tmpfs", f"/work:rw,nosuid,nodev,size={self.work_mb}m,uid=1000,gid=1000,mode=0700",
                   *(["--runtime", self.runtime] if self.runtime else []), *mounts,
                   "--entrypoint", "python", self.image, "-u", "-I", "/sandbox/runner.py"]
            timed_out = False
            try:
                # The runner stops the code at its limit; this covers a container that does not end by itself.
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=spec.timeout_s + 60)
                rc, text = p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
            except subprocess.TimeoutExpired as e:
                timed_out = True
                subprocess.run([self.docker, "kill", name], capture_output=True, timeout=60)
                rc, text = 124, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            got = subprocess.run([self.docker, "inspect", "-f", "{{.State.OOMKilled}}", name], capture_output=True,
                                 text=True, timeout=60)
            oom = got.stdout.strip() == "true"
            out = _finish(rc, text, spec, oom, timed_out)
            out["egress_bytes"] = policy.used if policy is not None else 0
            return out
        finally:
            if proxy is not None:
                proxy.close()
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=60)
            shutil.rmtree(root, ignore_errors=True)


class KubeSandbox:
    """A pod per run: the program and inputs in a Secret, the token in another, a NetworkPolicy that denies all
    traffic (or allows only Cairn's egress proxy), all deleted after the run."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"
    LABEL = "cairn.io/sandbox-run"

    def __init__(self, namespace: str, image: str = IMAGE, api: str = "", token_file: str = "", ca_file: str = "",
                 runtime_class: str = "", proxy_url: str = "", proxy_peers: Optional[list] = None,
                 creds: Optional[ProxyCredentials] = None, poll: float = 1.0):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise SandboxError("sandboxes on Kubernetes: no API server (not in a cluster, and no "
                               "CAIRN_SANDBOX_K8S_API)", 503)
        self.namespace, self.image, self.runtime_class, self.poll = namespace, image, runtime_class, poll
        self.proxy_url, self.proxy_peers, self.creds = proxy_url, proxy_peers or [], creds
        self.token_file = token_file or f"{self.SA}/token"
        self.verify: Any = ca_file or (f"{self.SA}/ca.crt" if os.path.exists(f"{self.SA}/ca.crt") else True)

    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, raw: bool = False) -> Any:
        import httpx

        token = open(self.token_file).read().strip()
        with httpx.Client(base_url=self.api, verify=self.verify, timeout=30,
                          headers={"Authorization": f"Bearer {token}"}) as c:
            r = c.request(method, path, json=body)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise SandboxError(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}", 502)
        return r.text if raw else (r.json() if r.content else {})

    def manifests(self, spec: Spec, network: bool, proxy_cred: str = "") -> list[dict]:
        name, rid = f"cairn-sbx-{spec.run_id[:20]}", spec.run_id[:20]
        labels = {"app.kubernetes.io/part-of": "cairn", "app.kubernetes.io/component": "sandbox", self.LABEL: rid}
        program = {k: base64.b64encode(v.encode()).decode() for k, v in spec.program().items()}
        items = [{"key": k, "path": k} for k in program]
        for i, (fname, data) in enumerate(sorted(spec.files.items())):
            program[f"in-{i}"] = base64.b64encode(data).decode()
            items.append({"key": f"in-{i}", "path": f"inputs/{fname}"})
        env = spec.runner_env()
        if network:
            u = urlsplit(self.proxy_url)
            proxy = f"{u.scheme}://run:{proxy_cred}@{u.netloc}"
            env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
        memory = f"{spec.memory_mb}Mi"
        pod = {
            "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
            "activeDeadlineSeconds": spec.timeout_s + 30,
            **({"runtimeClassName": self.runtime_class} if self.runtime_class else {}),
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "sandbox", "image": self.image, "command": ["python", "-u", "-I", "/sandbox/runner.py"],
                "envFrom": [{"secretRef": {"name": f"{name}-env"}}],
                "resources": {"requests": {"cpu": str(spec.cpus), "memory": memory},
                              "limits": {"cpu": str(spec.cpus), "memory": memory}},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [{"name": "program", "mountPath": "/sandbox", "readOnly": True},
                                 {"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "program", "secret": {"secretName": name, "items": items, "defaultMode": 0o444}},
                        {"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
                        {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}]}
        egress = [{"to": self.proxy_peers, "ports": [{"port": urlsplit(self.proxy_url).port or 3128,
                                                      "protocol": "TCP"}]}] if network else []
        return [
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "labels": labels},
             "spec": {"podSelector": {"matchLabels": {self.LABEL: rid}}, "policyTypes": ["Ingress", "Egress"],
                      "ingress": [], "egress": egress}},
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": name, "labels": labels},
             "data": program},
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": f"{name}-env", "labels": labels},
             "stringData": env},
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "labels": labels}, "spec": pod},
        ]

    def run(self, spec: Spec, policy: Optional[Policy]) -> dict:
        network = policy is not None
        if network and (not self.proxy_url or not self.proxy_peers or self.creds is None):
            raise SandboxError("network for Kubernetes sandboxes needs Cairn's egress proxy "
                               "(CAIRN_SANDBOX_K8S_PROXY_URL, CAIRN_SANDBOX_K8S_PROXY_PEERS, CAIRN_SANDBOX_PROXY_KEY)", 503)
        if sum(len(v) for v in spec.files.values()) > 700_000:
            raise SandboxError("on Kubernetes a run's input files are at most 700 KB together", 413)
        cred = self.creds.issue(sorted(policy.allow), policy.cairn is not None, spec.timeout_s + 120) if network else ""
        ns, name = self.namespace, f"cairn-sbx-{spec.run_id[:20]}"
        paths = {"NetworkPolicy": f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies",
                 "Secret": f"/api/v1/namespaces/{ns}/secrets", "Pod": f"/api/v1/namespaces/{ns}/pods"}
        try:
            for obj in self.manifests(spec, network, cred):   # the policy first: the pod never runs unfenced
                self._call("POST", paths[obj["kind"]], obj)
            end, timed_out, oom = time.time() + spec.timeout_s + 300, False, False
            while True:
                pod = self._call("GET", f"{paths['Pod']}/{name}")
                st = pod.get("status") or {}
                if st.get("phase") in ("Succeeded", "Failed"):
                    timed_out = st.get("reason") == "DeadlineExceeded"
                    for cs in st.get("containerStatuses") or []:
                        term = (cs.get("state") or {}).get("terminated") or {}
                        oom = oom or term.get("reason") == "OOMKilled"
                    break
                if time.time() > end:
                    timed_out = True
                    break
                time.sleep(self.poll)
            text = self._call("GET", f"{paths['Pod']}/{name}/log", ok404=True, raw=True) or ""
            rc = 0 if (pod.get("status") or {}).get("phase") == "Succeeded" else 1
            return {**_finish(rc, text, spec, oom, timed_out), "egress_bytes": None}
        finally:
            for path in (f"{paths['Pod']}/{name}?gracePeriodSeconds=0", f"{paths['Secret']}/{name}",
                         f"{paths['Secret']}/{name}-env", f"{paths['NetworkPolicy']}/{name}"):
                try:
                    self._call("DELETE", path, ok404=True)
                except Exception:  # noqa: BLE001 - the token in it is revoked at the run's end anyway
                    logger.warning("could not delete %s", path)


def runtime_from_env() -> Optional[Any]:
    e = os.environ.get
    rt = e("CAIRN_SANDBOX_RUNTIME", "")
    image = e("CAIRN_SANDBOX_IMAGE", IMAGE)
    if rt == "docker":
        return DockerSandbox(image, work_mb=int(e("CAIRN_SANDBOX_WORK_MB", "256")),
                             runtime=e("CAIRN_SANDBOX_DOCKER_RUNTIME", ""))
    if rt == "kubernetes":
        peers = e("CAIRN_SANDBOX_K8S_PROXY_PEERS", "")
        key = (e("CAIRN_SANDBOX_PROXY_KEY") or "").encode()
        return KubeSandbox(e("CAIRN_SANDBOX_K8S_NAMESPACE", "cairn-sandboxes"), image, e("CAIRN_SANDBOX_K8S_API", ""),
                           e("CAIRN_SANDBOX_K8S_TOKEN_FILE", ""), e("CAIRN_SANDBOX_K8S_CA_FILE", ""),
                           e("CAIRN_SANDBOX_K8S_RUNTIME_CLASS", ""), e("CAIRN_SANDBOX_K8S_PROXY_URL", ""),
                           json.loads(peers) if peers else None, ProxyCredentials(key) if key else None)
    if rt:
        raise ValueError("CAIRN_SANDBOX_RUNTIME: docker or kubernetes")
    return None


# --------------------------------------------------------------------------- the service
class Sandboxes:
    def __init__(self, engine, runtime, authorize: Callable[[str, str, Optional[str]], bool],
                 is_operator: Callable[[str], bool], agents=None, cairn_url: str = "", audit=None):
        """``authorize(actor, privilege, urn)``: the PDP; ``agents``: the agent hub (tokens for data access);
        ``cairn_url``: where the egress proxy finds Cairn for ``network.cairn``."""
        e = os.environ.get
        self.engine, self.runtime, self.authorize, self.is_operator = engine, runtime, authorize, is_operator
        self.agents, self.cairn_url, self.audit = agents, cairn_url.rstrip("/"), audit
        self.cairn_ca = e("CAIRN_SANDBOX_CAIRN_CA_FILE", "")
        self.allowed_hosts = {h.strip().lower() for h in e("CAIRN_SANDBOX_ALLOWED_HOSTS", "").split(",") if h.strip()}
        self.allow_private = _env_bool("CAIRN_SANDBOX_ALLOW_PRIVATE_EGRESS")
        self.max_egress = int(float(e("CAIRN_SANDBOX_MAX_EGRESS_MB", "100")) * (1 << 20))
        self.max_timeout = int(e("CAIRN_SANDBOX_MAX_TIMEOUT_S", "600"))
        self.max_memory = int(e("CAIRN_SANDBOX_MAX_MEMORY_MB", "2048"))
        self.max_cpus = float(e("CAIRN_SANDBOX_MAX_CPUS", "2"))
        self.per_person = int(e("CAIRN_SANDBOX_MAX_PER_PERSON", "2"))
        self.max_running = int(e("CAIRN_SANDBOX_MAX_RUNNING", "8"))
        self.max_input = int(float(e("CAIRN_SANDBOX_MAX_INPUT_MB", "20")) * (1 << 20))
        self.max_output = int(float(e("CAIRN_SANDBOX_MAX_OUTPUT_MB", "5")) * (1 << 20))
        self.keep_days = int(e("CAIRN_SANDBOX_KEEP_DAYS", "30"))
        self._lock = threading.Lock()

    def limits(self) -> dict:
        return {"configured": self.runtime is not None, "languages": list(LANGUAGES),
                "max_timeout_s": self.max_timeout, "max_memory_mb": self.max_memory, "max_cpus": self.max_cpus,
                "max_per_person": self.per_person, "max_running": self.max_running,
                "max_input_bytes": self.max_input, "max_output_bytes": self.max_output,
                "allowed_hosts": sorted(self.allowed_hosts), "cairn_access": bool(self.cairn_url and self.agents)}

    # ---- checking a request ------------------------------------------------------------------------
    def _check(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise SandboxError("a run is an object: language, code, files, network, limits")
        unknown = sorted(set(body) - {"language", "code", "files", "network", "limits"})
        if unknown:
            raise SandboxError(f"unknown fields {unknown}")
        language = body.get("language", "python")
        if language not in LANGUAGES:
            raise SandboxError(f"language: one of {', '.join(LANGUAGES)}")
        code = body.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 200_000:
            raise SandboxError("code: the program, up to 200,000 characters")
        files: dict[str, bytes] = {}
        raw = body.get("files") or []
        if isinstance(raw, dict):
            raw = [{"name": k, "content": v} for k, v in raw.items()]
        if not isinstance(raw, list) or len(raw) > 100:
            raise SandboxError("files: up to 100 files, each {name, content (base64)}")
        for f in raw:
            name = f.get("name") if isinstance(f, dict) else None
            if not isinstance(name, str) or not FILE_NAME.match(name) or ".." in name.split("/") or name.startswith("out/"):
                raise SandboxError(f"files: {name!r} is not a plain relative file name")
            try:
                files[name] = base64.b64decode(str(f.get("content") or ""), validate=True)
            except ValueError:
                raise SandboxError(f"files: {name}: content is base64")
        size = sum(len(v) for v in files.values())
        if size > self.max_input:
            raise SandboxError(f"the input files are over {self.max_input} bytes together", 413)
        net = body.get("network") or {}
        if not isinstance(net, dict) or set(net) - {"cairn", "allow"}:
            raise SandboxError("network: {cairn: true|false, allow: [host or host:port]}")
        allow = net.get("allow") or []
        if not isinstance(allow, list) or len(allow) > 20 or not all(
                isinstance(h, str) and HOST.match(h.lower()) and not _is_address(h.rsplit(":", 1)[0]) for h in allow):
            raise SandboxError("network.allow: up to 20 host names (host or host:port), no addresses or wildcards")
        allow = sorted({h.lower() for h in allow})
        refused = [h for h in allow if h not in self.allowed_hosts and h.split(":")[0] not in self.allowed_hosts]
        if refused:
            raise SandboxError(f"network.allow: {', '.join(refused)} may not be reached from sandboxes (an operator "
                               "allows hosts in CAIRN_SANDBOX_ALLOWED_HOSTS)", 403)
        lim = body.get("limits") or {}
        if not isinstance(lim, dict) or set(lim) - {"timeout_s", "memory_mb", "cpus"}:
            raise SandboxError("limits: {timeout_s, memory_mb, cpus}")

        def number(key, default, low, high, kind):
            v = lim.get(key, default)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
                raise SandboxError(f"limits.{key}: {low} to {high}")
            return kind(v)
        return {"language": language, "code": code, "files": files, "input_bytes": size,
                "network": {"cairn": bool(net.get("cairn")), "allow": allow},
                "timeout_s": number("timeout_s", min(60, self.max_timeout), 1, self.max_timeout, int),
                "memory_mb": number("memory_mb", min(512, self.max_memory), 64, self.max_memory, int),
                "cpus": number("cpus", min(1.0, self.max_cpus), 0.1, self.max_cpus, float)}

    # ---- running ---------------------------------------------------------------------------------
    def _claim(self, actor: str, agent: Optional[str], spec: dict) -> str:
        rid, now = uuid.uuid4().hex, _now()
        stale = now - (self.max_timeout + 600) * 1000     # a run "running" this long was lost
        with self._lock, self.engine.begin() as conn:
            running = and_(sandbox_runs.c.status == "running", sandbox_runs.c.started_ms > stale)
            total = conn.execute(select(func.count()).select_from(sandbox_runs).where(running)).scalar() or 0
            mine = conn.execute(select(func.count()).select_from(sandbox_runs).where(
                and_(running, sandbox_runs.c.actor == actor))).scalar() or 0
            if mine >= self.per_person:
                raise SandboxError(f"you have {mine} sandbox runs going (at most {self.per_person} at a time)", 429)
            if total >= self.max_running:
                raise SandboxError("every sandbox is busy; try again shortly", 429)
            conn.execute(insert(sandbox_runs).values(
                id=rid, actor=actor, agent=agent, language=spec["language"], status="running",
                network=spec["network"], timeout_s=spec["timeout_s"], memory_mb=spec["memory_mb"], cpus=spec["cpus"],
                started_ms=now, input_bytes=spec["input_bytes"]))
            conn.execute(update(sandbox_runs).where(and_(sandbox_runs.c.status == "running",
                                                         sandbox_runs.c.started_ms <= stale)).values(status="lost"))
            conn.execute(delete(sandbox_runs).where(sandbox_runs.c.started_ms < now - self.keep_days * 86_400_000))
        return rid

    def _token(self, p, ttl_s: int) -> str:
        """A token for the person, narrowed to reading data (and to the asking agent's scopes and intent)."""
        if self.agents is None or not self.cairn_url:
            raise SandboxError("data access from sandboxes is not configured (the agent hub and "
                               "CAIRN_SANDBOX_CAIRN_URL)", 503)
        scopes = list(DATA_SCOPES)
        if getattr(p, "via", "") == "agent":
            if getattr(p, "identity", "person") != "person":
                raise SandboxError("this identity may not read data from a sandbox", 403)
            scopes = [s for s in scopes if s in (p.scopes or ())]
            if "query_data" not in scopes:
                raise SandboxError("this agent's scopes do not include query_data", 403)
        self.agents.system_agent(AGENT_ID, "Cairn code sandbox", list(DATA_SCOPES),
                                 "Code run in Cairn's sandboxes reads data as the person it runs for.")
        why = self.agents.runnable(AGENT_ID)
        if why:
            raise SandboxError(why, 403)
        return self.agents.issue_run_token(AGENT_ID, p.urn, ttl_ms=ttl_s * 1000, intent=getattr(p, "intent", None),
                                           scopes=scopes)

    def run(self, p, body: Any) -> dict:
        if getattr(p, "is_service", False) or getattr(p, "via", "") == "service":
            raise SandboxError("sandboxes run code for people and their agents", 403)
        actor = p.urn
        agent = getattr(p, "agent", None) if getattr(p, "via", "") == "agent" else None
        if not self.authorize(actor, PRIVILEGE, None):
            raise SandboxError("running code in a sandbox needs RUN_CODE", 403)
        if self.runtime is None:
            raise SandboxError("sandboxes are not configured: set CAIRN_SANDBOX_RUNTIME (docker or kubernetes)", 503)
        spec = self._check(body)
        token = self._token(p, spec["timeout_s"] + 120) if spec["network"]["cairn"] else None
        try:
            rid = self._claim(actor, agent, spec)
        except BaseException:
            if token:
                self.agents.revoke_token(token)
            raise
        env = {"CAIRN_SANDBOX_RUN": rid, "CAIRN_USER": actor.removeprefix("urn:li:corpuser:")}
        network = spec["network"]["cairn"] or bool(spec["network"]["allow"])
        policy = None
        if network:
            policy = Policy(spec["network"]["allow"], self.cairn_url if spec["network"]["cairn"] else "",
                            self.allow_private, self.max_egress, self.cairn_ca)
            proxy = f"http://127.0.0.1:{PROXY_PORT}"
            env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy, NO_PROXY="",
                       no_proxy="")
        if token:
            env.update(CAIRN_URL=f"http://{CAIRN_HOST}", CAIRN_TOKEN=token)
        run = Spec(rid, spec["language"], spec["code"], spec["files"], env, spec["timeout_s"], spec["memory_mb"],
                   spec["cpus"], spec["network"], self.max_output)
        started = _now()
        try:
            out = self.runtime.run(run, policy)
        except SandboxError as e:
            out = {"status": "failed", "exit_code": None, "stdout": "", "stderr": "", "result": None, "files": [],
                   "error": str(e)}
        except Exception as e:  # noqa: BLE001 - recorded on the run and reported, never left running
            logger.exception("sandbox run %s failed", rid)
            out = {"status": "failed", "exit_code": None, "stdout": "", "stderr": "", "result": None, "files": [],
                   "error": f"the sandbox could not run: {type(e).__name__}"}
        finally:
            if token:
                self.agents.revoke_token(token)
        finished = _now()
        files = [{"name": f["name"], "bytes": f["bytes"]} for f in out.get("files") or []]
        out_bytes = sum(f["bytes"] for f in files) + len(out.get("stdout") or "") + len(out.get("stderr") or "")
        with self.engine.begin() as conn:
            conn.execute(update(sandbox_runs).where(sandbox_runs.c.id == rid).values(
                status=out["status"], finished_ms=finished, duration_ms=finished - started,
                exit_code=out.get("exit_code"), output_bytes=out_bytes, egress_bytes=out.get("egress_bytes"),
                files=files, error=(out.get("error") or None) and str(out["error"])[:2000]))
        if self.audit is not None:
            self.audit.record("ai", f"sandbox.run_{out['status']}", actor, f"sandbox:{rid}", {
                "language": spec["language"], "network": spec["network"], "timeout_s": spec["timeout_s"],
                "memory_mb": spec["memory_mb"], "cpus": spec["cpus"], "ms": finished - started,
                "input_bytes": spec["input_bytes"], "output_bytes": out_bytes, "egress_bytes": out.get("egress_bytes"),
                "files": len(files), **({"agent": agent} if agent else {})})
        return {"id": rid, "status": out["status"], "exit_code": out.get("exit_code"), "result": out.get("result"),
                "stdout": out.get("stdout") or "", "stderr": out.get("stderr") or "", "files": out.get("files") or [],
                "skipped_files": out.get("skipped_files") or [], "truncated": bool(out.get("truncated")),
                "error": out.get("error"), "duration_ms": finished - started, "egress_bytes": out.get("egress_bytes"),
                "limits": {"timeout_s": spec["timeout_s"], "memory_mb": spec["memory_mb"], "cpus": spec["cpus"]},
                "network": spec["network"]}

    # ---- records and usage -------------------------------------------------------------------------
    @staticmethod
    def _public(r) -> dict:
        d = dict(r._mapping)
        return d

    def list(self, actor: str, limit: int = 100) -> list[dict]:
        q = select(sandbox_runs).order_by(sandbox_runs.c.started_ms.desc()).limit(max(1, min(limit, 500)))
        if not self.is_operator(actor):
            q = q.where(sandbox_runs.c.actor == actor)
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(q)]

    def get(self, actor: str, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(sandbox_runs).where(sandbox_runs.c.id == rid)).first()
        if r is None or (r.actor != actor and not self.is_operator(actor)):
            raise SandboxError("no such sandbox run", 404)
        return self._public(r)

    def usage(self, actor: str, days: int = 30) -> dict:
        """Runs per person and day: count, CPU seconds (duration × CPUs), memory (MB × seconds), egress, outcomes."""
        if not 1 <= days <= 400:
            raise SandboxError("days: 1 to 400")
        since = _now() - days * 86_400_000
        q = select(sandbox_runs).where(sandbox_runs.c.started_ms >= since)
        if not self.is_operator(actor):
            q = q.where(sandbox_runs.c.actor == actor)
        rows: dict[tuple[str, str], dict] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(q):
                day = time.strftime("%Y-%m-%d", time.gmtime(r.started_ms / 1000))
                u = rows.setdefault((r.actor, day), {"actor": r.actor, "day": day, "runs": 0, "cpu_seconds": 0.0,
                                                     "memory_mb_seconds": 0.0, "egress_bytes": 0, "output_bytes": 0,
                                                     "by_status": {}})
                secs = (r.duration_ms or 0) / 1000
                u["runs"] += 1
                u["cpu_seconds"] = round(u["cpu_seconds"] + secs * float(r.cpus), 3)
                u["memory_mb_seconds"] = round(u["memory_mb_seconds"] + secs * int(r.memory_mb), 1)
                u["egress_bytes"] += int(r.egress_bytes or 0)
                u["output_bytes"] += int(r.output_bytes or 0)
                u["by_status"][r.status] = u["by_status"].get(r.status, 0) + 1
        return {"days": days, "usage": sorted(rows.values(), key=lambda x: (x["day"], x["actor"]), reverse=True)}


def summarize_for_model(out: dict, preview: int = 2000) -> dict:
    """A run's answer as a tool result for a model: the result and logs, and each file's name and size (text files
    with a short preview), never whole binary files."""
    files = []
    for f in out.get("files") or []:
        item = {"name": f["name"], "bytes": f["bytes"]}
        try:
            text = base64.b64decode(f.get("content") or "").decode("utf-8")
            item["preview"] = text[:preview]
        except (ValueError, UnicodeDecodeError):
            pass
        files.append(item)
    return {"id": out.get("id"), "status": out.get("status"), "result": out.get("result"),
            "stdout": (out.get("stdout") or "")[-4000:], "stderr": (out.get("stderr") or "")[-2000:],
            "files": files, "error": out.get("error")}


def build_sandboxes_router(sb: Sandboxes, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/sandboxes", tags=["ai"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except SandboxError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/runs")
    async def run(body: dict = Body(...), p=Depends(current_principal)):
        """Runs the code in a new sandbox and answers when it has finished."""
        return await call(lambda: sb.run(p, body))

    @router.get("/runs")
    async def list_(limit: int = 100, p=Depends(current_principal)):
        return await call(lambda: {"runs": sb.list(p.urn, limit)})

    @router.get("/runs/{rid}")
    async def get(rid: str, p=Depends(current_principal)):
        return await call(lambda: sb.get(p.urn, rid))

    @router.get("/usage")
    async def usage(days: int = 30, p=Depends(current_principal)):
        return await call(lambda: sb.usage(p.urn, days))

    @router.get("/limits")
    async def limits(p=Depends(current_principal)):
        return sb.limits()

    return router
