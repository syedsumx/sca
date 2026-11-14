"""Connector syncs: an open connector image (the Airbyte protocol) run in a hardened container, its records landed
in governed lakehouse tables (L165, part 2; the declarative REST source is ``cairn.pipelines.rest_source``).

``POST /v1/connector-syncs`` with ``{"name", "image": "repo@sha256:<digest>", "config": {...}, "secret_scope",
"streams": [{"name", "target": "namespace.table", "mode": "append" | "upsert" | "replace"}], "egress": ["host[:port]"],
"cron": "0 * * * *"}``. A run starts the image with ``discover`` and then ``read`` and reads the JSON lines it prints
(``RECORD``, ``STATE``, ``LOG``, ``TRACE``). ``POST /v1/connector-syncs/spec``, ``/check`` and ``/discover`` run the
image's command of that name for a draft (or a saved sync) and answer with what it printed, parsed.

* **Images by digest, from the operator's list.** ``CAIRN_CONNECTOR_IMAGES`` is a comma-separated list of
  ``repo@sha256:<digest>``; a sync's image is exactly one of them (checked when it is saved and at every run). Tags
  are refused. Without the variable every route answers 503. Without a runtime (``CAIRN_CONNECTOR_RUNTIME=docker``;
  Kubernetes is not built yet, see :class:`ConnectorRuntime`) nothing runs: a run is refused, never run in Cairn's
  own process.
* **A hardened container.** No capabilities, no new privileges, a read-only root file system with in-memory ``/tmp``
  and working directory (``/work``), memory (``CAIRN_CONNECTOR_MEMORY_MB``, 1024), CPU (``CAIRN_CONNECTOR_CPUS``, 1)
  and process (``CAIRN_CONNECTOR_PIDS``, 256) limits, no logging driver (what it prints is kept nowhere by Docker).
  It has an IPC namespace and a ``/dev/shm`` of its own (connectors that start worker processes need one).
  It runs as the image's own user; ``CAIRN_CONNECTOR_USER`` (``uid:gid``) forces one for images that cope with it.
  ``CAIRN_CONNECTOR_DOCKER_RUNTIME`` names a sandboxing runtime such as ``runsc``.
* **Config and secrets.** ``config`` is the connector's configuration; a value written ``{"secret": "<key>"}`` is
  replaced at run time by that key of ``secret_scope``, read as the sync's owner. A literal value under a name that
  looks like a secret (password, token, api_key...) is refused when the sync is saved. The resolved config, the
  configured catalog and the state are files in a directory of the run's own (inside a directory only Cairn's user
  can enter), mounted read-only; nothing secret is on a command line, in a log, in an audit record, in an error or
  on the stored sync. The image is trusted with the config it is given: that is why the operator lists images.
* **Network.** None (``--network none``) unless the sync names ``egress`` hosts (``host`` or ``host:port``), each of
  which the operator lists in ``CAIRN_CONNECTOR_ALLOWED_HOSTS`` (otherwise the save is refused, and the run too if
  the list changed since). Then the container is on a network of its own made ``--internal`` (no route out), and
  the one address it reaches is that network's gateway, where Cairn's egress proxy for the run listens
  (``HTTP_PROXY``/``HTTPS_PROXY`` in the container, with the run's credentials); the proxy lets through the sync's
  hosts only, resolves names on Cairn's side and refuses private addresses unless
  ``CAIRN_CONNECTOR_ALLOW_PRIVATE_EGRESS=true``. Traffic is capped (``CAIRN_CONNECTOR_MAX_EGRESS_MB``, 1024). The
  gateway address is the host's: what the host serves on every interface is reachable there too, unless
  ``CAIRN_CONNECTOR_HOST_FIREWALL=iptables`` (Cairn then admits, from that network, only the proxy's port; it needs
  the right to change the host's firewall, and a run is refused when the rule cannot be set). Only connectors that
  honour the proxy variables get out: one that opens its own TCP connections (a database driver) does not.
* **Landing.** Each stream goes to its ``target``: ``append``; ``upsert`` by the stream's primary key (the sync's
  ``primary_key`` or the catalog's; the key's values are kept in ``_key``); or ``replace`` (the table becomes what
  the run read, in one commit at its end). Columns are the record's top-level fields as text (objects and lists as
  JSON) and ``_synced_ms``; a new field adds a column. Records are committed in batches
  (``CAIRN_CONNECTOR_BATCH_ROWS``, 5000) at the connector's ``STATE`` messages, and a state is saved only once every
  record before it has been committed: a failed run keeps what it committed with its state and the next run resumes
  there, skipping nothing. What a failed run had read since its last state is dropped (and read again).
* **Governed.** The owner needs ``WRITE_DATASET`` on every target, checked at save and at every run. Only the owner
  sees and changes a sync. Every run is audited (counts and names; never config values, records or what the
  connector logged); a failed run is recorded on the sync and told to the owner (``saas.sync_failed``).
* **Limits.** A run's time (``CAIRN_CONNECTOR_TIMEOUT_S``, 3600; ``spec``, ``check`` and ``discover``:
  ``CAIRN_CONNECTOR_ACTION_TIMEOUT_S``, 300), its records (``CAIRN_CONNECTOR_MAX_RECORDS``, 5,000,000), a line of its
  output (``CAIRN_CONNECTOR_MAX_LINE_BYTES``, 4 MB; a longer line fails the run) and its whole output
  (``CAIRN_CONNECTOR_MAX_OUTPUT_MB``, 10240). A sync's ``limits`` (``timeout_s``, ``max_records``) lower the first
  two. A line that is not a JSON protocol message fails the run. ``LOG`` and ``TRACE`` lines are counted, never
  kept; of an error ``TRACE`` only the last one's message, with every resolved secret value removed and cut to 300
  characters. ``CAIRN_CONNECTOR_CONCURRENCY`` (2) runs at a time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as _secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
TARGET = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}$")
# repo@sha256:<digest>: an optional registry host (with a port), a path, and a digest. No tag.
IMAGE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
                   r"@sha256:[0-9a-f]{64}$")
HOST = re.compile(r"^(?=.{1,253}(:\d{1,5})?$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
                  r"(:\d{1,5})?$")
SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# A config name under which a literal value is refused: it is written as {"secret": "<key>"} instead.
SECRET_NAME = re.compile(r"passw(or)?d|passphrase|(^|[_.-])pass$|secret|token|api[_-]?key|private[_-]?key|credential|access[_-]?key",
                         re.IGNORECASE)
USER = re.compile(r"^\d{1,10}(:\d{1,10})?$")
MODES = ("append", "upsert", "replace")
SYNC_MODES = ("incremental", "full_refresh")
COMMANDS = ("spec", "check", "discover", "read")
MOUNT = "/cairn"                # where the run's files are inside the container
KEY, SYNCED = "_key", "_synced_ms"
MAX_CONFIG_BYTES, MAX_STATE_BYTES = 256 << 10, 4 << 20

connector_syncs = Table(
    "cairn_connector_syncs", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("image", String(512), nullable=False),
    Column("config", JSONText, nullable=False),               # as written: secrets are {"secret": "<key>"} references
    Column("secret_scope", String(128), nullable=True),
    Column("streams", JSONText, nullable=False),              # [{"name", "target", "mode", ...}]
    Column("egress", JSONText, nullable=False),               # ["host[:port]"]
    Column("limits", JSONText, nullable=True),                # {"timeout_s", "max_records"}
    Column("cron", String(100), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("state", Text, nullable=True),                     # the connector's last committed state (JSON)
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("running", Boolean, nullable=False, default=False),
    Column("last_run_ms", BigInteger, nullable=True),
    Column("last_status", String(10), nullable=True),
    Column("last_rows", Integer, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("last_stats", JSONText, nullable=True),            # counts of what the last run printed
    Column("created_ms", BigInteger, nullable=False),
)


class ConnectorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- request models (at module level: the router's annotations are read from here) ------------------------------
class StreamBody(BaseModel):
    name: str
    target: str
    mode: str = "append"
    namespace: Optional[str] = None
    primary_key: Optional[list[str]] = None
    cursor_field: Optional[list[str]] = None
    sync_mode: Optional[str] = None


class SyncBody(BaseModel):
    name: Optional[str] = None
    image: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_scope: Optional[str] = None
    streams: list[StreamBody]
    egress: list[str] = Field(default_factory=list)
    limits: Optional[dict[str, int]] = None
    cron: Optional[str] = None
    enabled: bool = True


class DraftBody(BaseModel):
    """What ``spec``, ``check`` and ``discover`` run for: a saved sync (``sync``), or an image with a config."""
    sync: Optional[str] = None
    image: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)
    secret_scope: Optional[str] = None
    egress: list[str] = Field(default_factory=list)


def _now() -> int:
    return int(time.time() * 1000)


def _next(cron: Optional[str], after_ms: int) -> Optional[int]:
    if not cron:
        return None
    from croniter import croniter

    return int(croniter(cron, datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)).get_next(float) * 1000)


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- config, secrets, redaction
def _is_ref(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"secret"} and isinstance(v["secret"], str)


def secret_refs(config: Any, path: str = "config") -> list[str]:
    """The secret keys a config refers to. Refuses a reference that is not a key's name, and a literal value under
    a name that looks like a secret (it would be stored on the sync)."""
    refs: list[str] = []
    if _is_ref(config):
        if not SECRET_KEY.match(config["secret"]):
            raise ConnectorError(f"{path}: a secret reference names a key of the secret scope")
        return [config["secret"]]
    if isinstance(config, dict):
        for k, v in config.items():
            if not isinstance(k, str):
                raise ConnectorError(f"{path}: names are text")
            if isinstance(v, str) and v and SECRET_NAME.search(k):
                raise ConnectorError(f'{path}.{k}: a secret is not written on the sync; put it in the secret scope '
                                     f'and write {{"secret": "<key>"}}')
            refs += secret_refs(v, f"{path}.{k}")
    elif isinstance(config, list):
        for i, v in enumerate(config):
            refs += secret_refs(v, f"{path}[{i}]")
    return refs


def resolve_config(config: Any, read: Callable[[str], str]) -> tuple[Any, list[str]]:
    """The config with every ``{"secret": key}`` replaced by ``read(key)``, and the values read."""
    values: list[str] = []

    def walk(v: Any) -> Any:
        if _is_ref(v):
            got = read(v["secret"])
            values.append(got)
            return got
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(config), values


def redact(text: str, values: list[str]) -> str:
    """``text`` with every secret value (and its JSON-escaped form) replaced."""
    forms = {form for v in values if v for form in (v, json.dumps(v)[1:-1])}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


def redact_all(doc: Any, values: list[str]) -> Any:
    if isinstance(doc, str):
        return redact(doc, values)
    if isinstance(doc, dict):
        return {redact(str(k), values): redact_all(v, values) for k, v in doc.items()}
    if isinstance(doc, list):
        return [redact_all(v, values) for v in doc]
    return doc


def column(field: str, i: int = 0) -> str:
    """A record's field as a column name (lowercase letters, digits and _)."""
    from cairn.pipelines.pipes import column_name

    return column_name(field, i)


def text(v: Any) -> Optional[str]:
    """A record's value as text: objects and lists as JSON, true/false as JSON writes them."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- runtimes
class ConnectorJob:
    """One run of an image's command, as a runtime needs it. ``files`` (name -> bytes) are what the command reads
    (``config.json``, ``catalog.json``, ``state.json``); ``egress`` the hosts it may reach (none: no network)."""

    def __init__(self, image: str, command: str, files: dict[str, bytes], egress: list[str], timeout_s: int,
                 memory_mb: int, cpus: float, max_line: int, max_output: int):
        if command not in COMMANDS:
            raise ConnectorError(f"command: one of {', '.join(COMMANDS)}")
        self.run_id = uuid.uuid4().hex
        self.image, self.command, self.files, self.egress = image, command, files, egress
        self.timeout_s, self.memory_mb, self.cpus = timeout_s, memory_mb, cpus
        self.max_line, self.max_output = max_line, max_output

    def args(self) -> list[str]:
        out = [self.command]
        for flag, name in (("--config", "config.json"), ("--catalog", "catalog.json"), ("--state", "state.json")):
            if name in self.files:
                out += [flag, f"{MOUNT}/{name}"]
        return out


class ConnectorExit:
    def __init__(self, code: int, timed_out: bool = False, oom: bool = False, egress_bytes: int = 0,
                 stdout_bytes: int = 0, stderr_bytes: int = 0):
        self.code, self.timed_out, self.oom = code, timed_out, oom
        self.egress_bytes, self.stdout_bytes, self.stderr_bytes = egress_bytes, stdout_bytes, stderr_bytes


class ConnectorRuntime(Protocol):
    """Where a connector's container runs. ``run`` starts the job's image with its command and files, hands every
    line of its standard output to ``on_line`` as it is printed (which may raise: the container is then stopped and
    the error raised on), enforces the job's limits and network, and leaves nothing behind. A Kubernetes runtime (a
    pod per run, a Secret for the files, a NetworkPolicy that allows only the egress proxy) is the same interface."""

    def run(self, job: ConnectorJob, on_line: Callable[[bytes], None]) -> ConnectorExit: ...


def pump(stream, on_line: Callable[[bytes], None], max_line: int, max_output: int) -> int:
    """Reads ``stream`` line by line into ``on_line``; refuses a line over ``max_line`` bytes and more than
    ``max_output`` bytes in all. Returns the bytes read."""
    total = 0
    while True:
        line = stream.readline(max_line + 2)
        if not line:
            return total
        total += len(line)
        if len(line) > max_line + 1 or (len(line) == max_line + 1 and not line.endswith(b"\n")):
            raise ConnectorError(f"the connector printed a line over {max_line} bytes "
                                 "(CAIRN_CONNECTOR_MAX_LINE_BYTES)", 502)
        if total > max_output:
            raise ConnectorError(f"the connector printed over {max_output} bytes (CAIRN_CONNECTOR_MAX_OUTPUT_MB)", 502)
        if line.strip():
            on_line(line)


class DockerConnectorRuntime:
    """A new container per run. No network interface, or (with egress) an ``--internal`` network of the run's own
    whose gateway address is where the run's egress proxy listens."""

    def __init__(self, docker: str = "docker", user: str = "", runtime: str = "", pids: int = 256, work_mb: int = 256,
                 allow_private: bool = False, max_egress: int = 1 << 30, pull_timeout_s: int = 600,
                 host_firewall: str = ""):
        if user and not USER.match(user):
            raise ValueError("CAIRN_CONNECTOR_USER: uid or uid:gid (numbers)")
        if host_firewall not in ("", "iptables"):
            raise ValueError("CAIRN_CONNECTOR_HOST_FIREWALL: iptables (or unset)")
        self.docker, self.user, self.runtime, self.pids, self.work_mb = docker, user, runtime, pids, work_mb
        self.allow_private, self.max_egress, self.pull_timeout_s = allow_private, max_egress, pull_timeout_s
        self.host_firewall = host_firewall

    def _docker(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout)

    def _pull(self, image: str) -> None:
        """The image by its digest: there already, or pulled (Docker checks what it pulls against the digest)."""
        if self._docker("image", "inspect", image).returncode == 0:
            return
        try:
            got = self._docker("pull", image, timeout=self.pull_timeout_s)
        except subprocess.TimeoutExpired:
            raise ConnectorError(f"pulling {image} took over {self.pull_timeout_s} s", 502)
        if got.returncode != 0:
            logger.warning("could not pull %s: %s", image, (got.stderr or "").strip()[-500:])
            raise ConnectorError(f"the image {image} could not be pulled (see Cairn's log)", 502)

    def _network(self, job: ConnectorJob, name: str) -> tuple[Any, dict[str, str], Callable[[], None], Any]:
        """The run's network: (its name, the proxy variables, how to take it down, the egress policy)."""
        from cairn.ai.sandboxes import Policy, ProxyCredentials, TcpProxy

        bridge = "cx" + job.run_id[:12]
        made = self._docker("network", "create", "--internal", "--driver", "bridge",
                            "-o", f"com.docker.network.bridge.name={bridge}", name)
        if made.returncode != 0:
            logger.warning("could not create the network %s: %s", name, (made.stderr or "").strip()[-500:])
            raise ConnectorError("the run's network could not be created (see Cairn's log)", 503)
        undo: list[Callable[[], Any]] = [lambda: self._docker("network", "rm", name)]

        def close() -> None:
            for fn in reversed(undo):
                try:
                    fn()
                except Exception:  # noqa: BLE001 - the rest is still taken down
                    logger.exception("could not take down part of the network %s", name)
        try:
            got = self._docker("network", "inspect", "-f", "{{.Internal}} {{(index .IPAM.Config 0).Gateway}}", name)
            internal, _, gateway = got.stdout.strip().partition(" ")
            if got.returncode != 0 or internal != "true" or not gateway:
                raise ConnectorError("the run's network is not an internal one with a gateway address", 503)
            policy = Policy(job.egress, "", self.allow_private, self.max_egress)
            creds = ProxyCredentials(_secrets.token_bytes(32))          # this run's alone

            class RunProxy(TcpProxy):                                   # one policy (and one byte count) for the run
                def _policy(self, headers: dict):
                    return policy if self.creds.check(headers.get("proxy-authorization", "")) is not None else None
            proxy = RunProxy(f"{gateway}:0", creds, "", self.allow_private, self.max_egress)
            undo.append(proxy.close)
            if self.host_firewall == "iptables":
                # From this network the host answers on the proxy's port only (the accept rule ends up first).
                rules = [["INPUT", "-i", bridge, "-j", "DROP"],
                         ["INPUT", "-i", bridge, "-p", "tcp", "--dport", str(proxy.port), "-j", "ACCEPT"]]
                for rule in rules:
                    try:
                        r = subprocess.run(["iptables", "-w", "5", "-I", *rule], capture_output=True, text=True, timeout=30)
                        refused = (r.stderr or "").strip()[-300:] if r.returncode != 0 else ""
                    except (OSError, subprocess.TimeoutExpired) as e:
                        refused = type(e).__name__
                    if refused:
                        logger.warning("iptables refused %s: %s", rule, refused)
                        raise ConnectorError("the host firewall rule for the run's network could not be set "
                                             "(CAIRN_CONNECTOR_HOST_FIREWALL=iptables)", 503)
                    undo.append(lambda rule=rule: subprocess.run(["iptables", "-w", "5", "-D", *rule],
                                                                 capture_output=True, timeout=30))
            url = f"http://run:{creds.issue(sorted(policy.allow), False, job.timeout_s + 300)}@{gateway}:{proxy.port}"
            env = {k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
            return name, env, close, policy
        except BaseException:
            close()
            raise

    def run(self, job: ConnectorJob, on_line: Callable[[bytes], None]) -> ConnectorExit:
        self._pull(job.image)
        root = tempfile.mkdtemp(prefix=f"cairn-cx-{job.run_id[:12]}-")     # 0700: only Cairn's user enters
        name = f"cairn-cx-{job.run_id}"
        close_network: Callable[[], None] = lambda: None                   # noqa: E731
        proc, timer, policy = None, None, None
        state = {"timed_out": False}
        try:
            # The files are readable by the image's user, whoever it is (the container has no capability to read
            # another user's files); on the host they are behind the run's directory, which only Cairn's user enters.
            files = os.path.join(root, "in")
            os.mkdir(files, 0o755)
            os.chmod(files, 0o755)
            for fname, data in job.files.items():
                fd = os.open(os.path.join(files, fname), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.chmod(os.path.join(files, fname), 0o644)
            network, env = "none", {}
            if job.egress:
                network, env, close_network, policy = self._network(job, name + "-net")
            env_file = os.path.join(root, "env")
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:               # the proxy's credentials: a file docker reads, no command line
                for k, v in env.items():
                    fh.write(f"{k}={v}\n")
            cmd = [self.docker, "run", "--name", name, "--network", network, "--env-file", env_file,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                   "--ipc", "private", "--shm-size", "64m",   # its own /dev/shm: worker processes need one
                   "--log-driver", "none", "--pids-limit", str(self.pids),
                   "--memory", f"{job.memory_mb}m", "--memory-swap", f"{job.memory_mb}m", "--cpus", str(job.cpus),
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                   "--tmpfs", f"/work:rw,nosuid,nodev,size={self.work_mb}m,mode=1777", "--workdir", "/work",
                   *(["--user", self.user] if self.user else []),
                   *(["--runtime", self.runtime] if self.runtime else []),
                   "-v", f"{files}:{MOUNT}:ro", job.image, *job.args()]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            stderr = {"bytes": 0}

            def drain() -> None:                          # what it prints on stderr is counted and dropped
                for chunk in iter(lambda: proc.stderr.read(65536), b""):
                    stderr["bytes"] += len(chunk)
            threading.Thread(target=drain, daemon=True, name="cairn-connector-stderr").start()

            def stop() -> None:
                state["timed_out"] = True
                self._docker("kill", name)
            timer = threading.Timer(job.timeout_s, stop)
            timer.daemon = True
            timer.start()
            try:
                read = pump(proc.stdout, on_line, job.max_line, job.max_output)
            except BaseException:
                self._docker("kill", name)
                proc.kill()
                raise
            code = proc.wait(timeout=120)
            got = self._docker("inspect", "-f", "{{.State.OOMKilled}}", name)
            return ConnectorExit(code, state["timed_out"], got.stdout.strip() == "true",
                                 policy.used if policy is not None else 0, read, stderr["bytes"])
        finally:
            if timer is not None:
                timer.cancel()
            if proc is not None and proc.poll() is None:
                proc.kill()
            self._docker("rm", "-f", name)
            close_network()
            shutil.rmtree(root, ignore_errors=True)


def runtime_from_env() -> Optional[ConnectorRuntime]:
    e = os.environ.get
    rt = e("CAIRN_CONNECTOR_RUNTIME", "")
    if rt == "docker":
        return DockerConnectorRuntime(user=e("CAIRN_CONNECTOR_USER", ""), runtime=e("CAIRN_CONNECTOR_DOCKER_RUNTIME", ""),
                                      pids=int(e("CAIRN_CONNECTOR_PIDS", "256")),
                                      work_mb=int(e("CAIRN_CONNECTOR_WORK_MB", "256")),
                                      allow_private=_env_bool("CAIRN_CONNECTOR_ALLOW_PRIVATE_EGRESS"),
                                      max_egress=int(float(e("CAIRN_CONNECTOR_MAX_EGRESS_MB", "1024")) * (1 << 20)),
                                      pull_timeout_s=int(e("CAIRN_CONNECTOR_PULL_TIMEOUT_S", "600")),
                                      host_firewall=e("CAIRN_CONNECTOR_HOST_FIREWALL", ""))
    if rt:
        raise ValueError("CAIRN_CONNECTOR_RUNTIME: docker (Kubernetes is not built yet)")
    return None


# --------------------------------------------------------------------------- landing one run's records
class _Landing:
    """One read's records on their way to the tables, and its state on its way to the sync: records are buffered
    per stream and committed at the connector's STATE messages; a state is saved only when everything read before
    it is committed."""

    def __init__(self, svc: "ConnectorSyncs", sync: dict, streams: list[dict], state: Optional[dict], max_records: int):
        self.svc, self.sync, self.max_records = svc, sync, max_records
        self.streams = {s["name"]: s for s in streams}
        self.buffers: dict[str, list[dict]] = {s["name"]: [] for s in streams}
        self.state = state                     # the last one saved
        self.pending = state                   # the last one read
        self.dirty = False
        self.records = 0                       # read
        self.rows = 0                          # committed
        self.touched: set[str] = set()

    def _waiting(self, replace: bool) -> int:
        return sum(len(b) for n, b in self.buffers.items() if (self.streams[n]["mode"] == "replace") == replace)

    def record(self, rec: Any) -> None:
        if not isinstance(rec, dict) or not isinstance(rec.get("data"), dict):
            raise ConnectorError("a RECORD without a stream's data object", 502)
        s = self.streams.get(str(rec.get("stream")))
        if s is None or (s.get("namespace") and rec.get("namespace") not in (None, s["namespace"])):
            raise ConnectorError("a RECORD for a stream the sync did not ask for", 502)
        self.records += 1
        if self.records > self.max_records:
            raise ConnectorError(f"over {self.max_records} records in one run (CAIRN_CONNECTOR_MAX_RECORDS)", 502)
        self.buffers[s["name"]].append(rec["data"])
        if self._waiting(False) >= self.svc.batch * 20:       # a connector that sends no state: memory stays bounded
            self.flush()

    def state_message(self, st: Any) -> None:
        if not isinstance(st, dict):
            raise ConnectorError("a STATE without a state object", 502)
        kind = st.get("type") or "LEGACY"
        if kind == "STREAM":
            try:
                desc = st["stream"]["stream_descriptor"]
                key = json.dumps([desc.get("namespace"), desc["name"]])
            except (KeyError, TypeError):
                raise ConnectorError("a stream STATE without its stream's name", 502)
            streams = dict((self.pending or {}).get("streams") or {})
            streams[key] = {"type": "STREAM", "stream": st["stream"]}
            self.pending = {"streams": streams}
        elif kind == "GLOBAL":
            self.pending = {"global": {"type": "GLOBAL", "global": st.get("global")}}
        elif kind == "LEGACY":
            self.pending = {"legacy": st.get("data")}
        else:
            raise ConnectorError("a STATE of a kind Cairn does not know", 502)
        self.dirty = True
        waiting = self._waiting(False)
        if waiting == 0 or waiting >= self.svc.batch:
            self.flush()

    def flush(self, end: bool = False) -> None:
        """Commits what is buffered (a replaced table only at the run's end), then saves the last state read, unless
        records of a replaced table are still waiting (they were read before it)."""
        for name, buf in self.buffers.items():
            s = self.streams[name]
            replace = s["mode"] == "replace"
            if replace and not end:
                continue
            if buf or replace:
                self.rows += self.svc._commit(s, buf)
                self.touched.add(s["target"])
                self.buffers[name] = []
        if self.dirty and self._waiting(True) == 0:
            raw = json.dumps(self.pending)
            if len(raw) > MAX_STATE_BYTES:
                raise ConnectorError(f"the connector's state is over {MAX_STATE_BYTES} bytes", 502)
            with self.svc.engine.begin() as conn:
                conn.execute(update(connector_syncs).where(connector_syncs.c.id == self.sync["id"]).values(state=raw))
            self.state, self.dirty = self.pending, False


def state_file(state: Optional[dict]) -> Optional[bytes]:
    """The saved state as the connector reads it back (``--state``): a list of STATE messages, or the legacy blob."""
    if not state:
        return None
    if "legacy" in state:
        return json.dumps(state["legacy"]).encode()
    if "global" in state:
        return json.dumps([state["global"]]).encode()
    return json.dumps(list((state.get("streams") or {}).values())).encode()


# --------------------------------------------------------------------------- the service
class ConnectorSyncs:
    def __init__(self, engine, catalog: dict, read_secret: Callable[[str, str, str], str],
                 may_write: Callable[[str, str], bool], runtime: Optional[ConnectorRuntime],
                 urn: Optional[Callable[[str, str], str]] = None, audit=None, changes=None, notify=None):
        """``read_secret(owner, scope, key)``: a secret, read as the owner. ``may_write(owner, urn)``: WRITE_DATASET.
        ``catalog``: pyiceberg REST catalog properties. ``runtime``: where containers run (none: nothing runs)."""
        e = os.environ.get
        self.engine, self.catalog_props, self.read_secret, self.may_write = engine, catalog, read_secret, may_write
        self.runtime = runtime
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
        self.audit, self.changes, self.notify = audit, changes, notify
        self.images = set(_env_list("CAIRN_CONNECTOR_IMAGES"))
        bad = sorted(i for i in self.images if not IMAGE.match(i))
        if bad:                                           # a tag in the operator's list: nothing is allowed by it
            raise ValueError(f"CAIRN_CONNECTOR_IMAGES: {bad[0]} is not repo@sha256:<digest>")
        self.allowed_hosts = {h.lower() for h in _env_list("CAIRN_CONNECTOR_ALLOWED_HOSTS")}
        self.timeout_s = int(e("CAIRN_CONNECTOR_TIMEOUT_S", "3600"))
        self.action_timeout_s = int(e("CAIRN_CONNECTOR_ACTION_TIMEOUT_S", "300"))
        self.max_records = int(e("CAIRN_CONNECTOR_MAX_RECORDS", "5000000"))
        self.max_line = int(e("CAIRN_CONNECTOR_MAX_LINE_BYTES", str(4 << 20)))
        self.max_output = int(float(e("CAIRN_CONNECTOR_MAX_OUTPUT_MB", "10240")) * (1 << 20))
        self.memory_mb = int(e("CAIRN_CONNECTOR_MEMORY_MB", "1024"))
        self.cpus = float(e("CAIRN_CONNECTOR_CPUS", "1"))
        self.batch = int(e("CAIRN_CONNECTOR_BATCH_ROWS", "5000"))
        self.pool = ThreadPoolExecutor(max_workers=int(e("CAIRN_CONNECTOR_CONCURRENCY", "2")),
                                       thread_name_prefix="cairn-connector")

    # ---- checking -------------------------------------------------------------------------------------
    def _configured(self) -> None:
        if not self.images:
            raise ConnectorError("connector syncs are not configured: an operator lists the images that may run in "
                                 "CAIRN_CONNECTOR_IMAGES (repo@sha256:<digest>)", 503)

    def _need_runtime(self) -> ConnectorRuntime:
        if self.runtime is None:
            raise ConnectorError("no runtime is configured for connector images (CAIRN_CONNECTOR_RUNTIME=docker); "
                                 "nothing is run", 503)
        return self.runtime

    def _image(self, image: Any) -> str:
        if not isinstance(image, str) or not IMAGE.match(image):
            raise ConnectorError("image: repo@sha256:<digest> (an image by its digest, not a tag)")
        if image not in self.images:
            raise ConnectorError("image: not one of the images an operator allows (CAIRN_CONNECTOR_IMAGES)", 403)
        return image

    def _egress(self, egress: Any) -> list[str]:
        if not isinstance(egress, list) or len(egress) > 20 or not all(
                isinstance(h, str) and HOST.match(h.lower()) for h in egress):
            raise ConnectorError("egress: up to 20 hosts (host or host:port), no wildcards")
        hosts = sorted({h.lower() for h in egress})
        refused = [h for h in hosts if h not in self.allowed_hosts and h.rsplit(":", 1)[0] not in self.allowed_hosts]
        if refused:
            logger.warning(json.dumps({"event": "egress.denied", "source": "connector", "target": refused,
                                       "reason": "not in CAIRN_CONNECTOR_ALLOWED_HOSTS"}))
            raise ConnectorError(f"egress: {', '.join(refused)} may not be reached by connectors (an operator allows "
                                 "hosts in CAIRN_CONNECTOR_ALLOWED_HOSTS)", 403)
        return hosts

    def _config(self, config: Any, scope: Any) -> tuple[dict, Optional[str]]:
        if not isinstance(config, dict) or len(json.dumps(config)) > MAX_CONFIG_BYTES:
            raise ConnectorError(f"config: the connector's configuration, an object of at most {MAX_CONFIG_BYTES} bytes")
        refs = secret_refs(config)
        scope = str(scope) if scope else None
        if refs and not scope:
            raise ConnectorError("secret_scope: the secret scope that holds the keys the config refers to")
        return config, scope

    def _streams(self, streams: Any) -> list[dict]:
        if not isinstance(streams, list) or not 1 <= len(streams) <= 200:
            raise ConnectorError("streams: 1 to 200 of {name, target, mode}")
        out = []
        for s in streams:
            if not isinstance(s, dict) or set(s) - {"name", "namespace", "target", "mode", "primary_key",
                                                    "cursor_field", "sync_mode"}:
                raise ConnectorError("streams: each {name, target, mode, namespace, primary_key, cursor_field, sync_mode}")
            name, target, mode = s.get("name"), str(s.get("target") or ""), s.get("mode", "append")
            if not isinstance(name, str) or not 1 <= len(name) <= 200:
                raise ConnectorError("streams.name: the stream's name in the connector's catalog")
            if not TARGET.match(target):
                raise ConnectorError(f"streams.target: namespace.table (lowercase), for {name}")
            if mode not in MODES:
                raise ConnectorError(f"streams.mode: {', '.join(MODES)}")
            one: dict[str, Any] = {"name": name, "target": target, "mode": mode}
            if s.get("namespace") is not None:
                if not isinstance(s["namespace"], str) or not s["namespace"]:
                    raise ConnectorError("streams.namespace: the stream's namespace in the catalog")
                one["namespace"] = s["namespace"]
            for k in ("primary_key", "cursor_field"):
                if s.get(k) is not None:
                    if not isinstance(s[k], list) or not s[k] or not all(isinstance(f, str) and f for f in s[k]):
                        raise ConnectorError(f"streams.{k}: a list of the record's fields")
                    one[k] = list(s[k])
            if s.get("sync_mode") is not None:
                if s["sync_mode"] not in SYNC_MODES:
                    raise ConnectorError(f"streams.sync_mode: {' or '.join(SYNC_MODES)}")
                if mode == "replace" and s["sync_mode"] != "full_refresh":
                    raise ConnectorError("streams: a replaced table is read whole (sync_mode full_refresh)")
                one["sync_mode"] = s["sync_mode"]
            out.append(one)
        for what in ("name", "target"):
            seen = [s[what] for s in out]
            twice = sorted({v for v in seen if seen.count(v) > 1})
            if twice:
                raise ConnectorError(f"streams: {twice[0]} is there twice ({what})")
        return out

    def _limits(self, limits: Any) -> Optional[dict]:
        if limits is None or limits == {}:
            return None
        if not isinstance(limits, dict) or set(limits) - {"timeout_s", "max_records"}:
            raise ConnectorError("limits: {timeout_s, max_records}")
        for k, top in (("timeout_s", self.timeout_s), ("max_records", self.max_records)):
            v = limits.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= top):
                raise ConnectorError(f"limits.{k}: 1 to {top}")
        return {k: v for k, v in limits.items() if v is not None} or None

    def _check(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise ConnectorError("a connector sync is an object: name, image, config, secret_scope, streams, egress, cron")
        config, scope = self._config({} if body.get("config") is None else body["config"], body.get("secret_scope"))
        cron = body.get("cron") or None
        if cron is not None:
            from croniter import croniter

            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise ConnectorError("cron: a cron expression (UTC)")
        return {"image": self._image(body.get("image")), "config": config, "secret_scope": scope,
                "streams": self._streams(body.get("streams")), "egress": self._egress(body.get("egress") or []),
                "limits": self._limits(body.get("limits")), "cron": cron, "enabled": bool(body.get("enabled", True))}

    def _urn(self, target: str) -> str:
        return self.urn(*target.split("."))

    def _need_write(self, owner: str, streams: list[dict]) -> None:
        for s in streams:
            if not self.may_write(owner, self._urn(s["target"])):
                raise ConnectorError(f"{owner} may not write {s['target']} (WRITE_DATASET)", 403)

    # ---- saving ---------------------------------------------------------------------------------------
    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        self._configured()
        cur = self._mine(actor, name) if name is not None else None     # only its owner learns it exists
        v = self._check(body)
        self._need_write(actor, v["streams"])
        now = _now()
        with self.engine.begin() as conn:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise ConnectorError("name: lowercase letters, digits and -")
                if conn.execute(select(connector_syncs.c.id).where(connector_syncs.c.name == name)).first():
                    raise ConnectorError(f"a connector sync {name} exists", 409)
                conn.execute(insert(connector_syncs).values(id=uuid.uuid4().hex, name=name, owner=actor, created_ms=now,
                                                            running=False, next_run_ms=_next(v["cron"], now), **v))
            else:
                changed: dict[str, Any] = dict(v)
                if cur["streams"] != v["streams"]:         # other streams, tables or modes: from the beginning
                    changed["state"] = None
                conn.execute(update(connector_syncs).where(connector_syncs.c.name == name)
                             .values(next_run_ms=_next(v["cron"], now), **changed))
        self._audit(actor, "connector.sync_saved", name, {
            "image": v["image"], "streams": [{k: s[k] for k in ("name", "target", "mode")} for s in v["streams"]],
            "egress": v["egress"]})
        return self.get(actor, name)

    def _get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(connector_syncs).where(connector_syncs.c.name == name)).first()
        if r is None:
            raise ConnectorError(f"no connector sync {name}", 404)
        return dict(r._mapping)

    @staticmethod
    def _public(s: dict) -> dict:
        """A sync as its owner sees it: not the connector's state (it is the connector's, and may be large)."""
        return {**{k: v for k, v in s.items() if k != "state"}, "has_state": bool(s.get("state"))}

    def _mine(self, actor: str, name: str) -> dict:
        s = self._get(name)
        if s["owner"] != actor:
            raise ConnectorError(f"no connector sync {name}", 404)
        return s

    def get(self, actor: str, name: str) -> dict:
        self._configured()
        return self._public(self._mine(actor, name))

    def list(self, actor: str) -> list[dict]:
        self._configured()
        with self.engine.connect() as conn:
            return [self._public(dict(r._mapping)) for r in conn.execute(
                select(connector_syncs).where(connector_syncs.c.owner == actor).order_by(connector_syncs.c.name))]

    def delete(self, actor: str, name: str) -> None:
        self._configured()
        self._mine(actor, name)
        with self.engine.begin() as conn:
            conn.execute(delete(connector_syncs).where(connector_syncs.c.name == name))
        self._audit(actor, "connector.sync_deleted", name, {})

    # ---- running an image's command -------------------------------------------------------------------
    def _resolve(self, owner: str, scope: Optional[str], config: dict) -> tuple[dict, list[str]]:
        """The config with its secrets, read from the scope as ``owner``. A key (or the scope) the owner cannot read
        stops the run before a container starts."""
        def read(key: str) -> str:
            try:
                return self.read_secret(owner, scope or "", key)
            except Exception as e:  # noqa: BLE001 - no scope, no key, or no right: fail closed
                raise ConnectorError(f"cannot read {key} from secret scope {scope} as {owner}: {e}", 403)
        return resolve_config(config, read)

    def _job(self, image: str, command: str, files: dict[str, bytes], egress: list[str], timeout_s: int) -> ConnectorJob:
        return ConnectorJob(image, command, files, egress, timeout_s, self.memory_mb, self.cpus, self.max_line,
                            self.max_output)

    def _exec(self, job: ConnectorJob, on_message: Callable[[str, dict], None], values: list[str],
              stats: Optional[dict] = None) -> dict:
        """Runs the job; every line must be a protocol message. Returns the counts of what was printed (kept in
        ``stats`` when the run fails too). Raises when the connector failed: a line that is no message, a limit,
        an error TRACE, or a status other than 0."""
        stats = {} if stats is None else stats
        stats.update({"records": 0, "states": 0, "logs": 0, "traces": 0, "other": 0, "lines": 0})
        last_error: list[str] = []

        def on_line(raw: bytes) -> None:
            stats["lines"] += 1
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                raise ConnectorError(f"line {stats['lines']} of the connector's output is not a protocol message "
                                     "(JSON with a type)", 502)
            kind = msg["type"]
            if kind == "LOG":
                stats["logs"] += 1
            elif kind == "TRACE":
                stats["traces"] += 1
                trace = msg.get("trace") if isinstance(msg.get("trace"), dict) else {}
                if trace.get("type") == "ERROR":
                    err = trace.get("error") if isinstance(trace.get("error"), dict) else {}
                    last_error[:] = [redact(str(err.get("message") or "an error"), values)[:300]]
            else:
                key = {"RECORD": "records", "STATE": "states"}.get(kind, "other")
                stats[key] += 1
                on_message(kind, msg)
        try:
            out = self._need_runtime().run(job, on_line)
        except subprocess.TimeoutExpired:
            raise ConnectorError("the container runtime did not answer in time", 503)
        except OSError as e:
            raise ConnectorError(f"the container runtime could not be used ({type(e).__name__})", 503)
        stats["output_bytes"], stats["egress_bytes"] = out.stdout_bytes, out.egress_bytes
        said = f": {last_error[0]}" if last_error else ""
        if out.timed_out:
            raise ConnectorError(f"{job.command} went over its time limit ({job.timeout_s} s) and was stopped", 504)
        if out.oom:
            raise ConnectorError(f"{job.command} went over its memory limit ({job.memory_mb} MB) and was stopped", 502)
        if out.code != 0:
            raise ConnectorError(f"the connector's {job.command} exited with status {out.code}{said}", 502)
        if last_error:
            raise ConnectorError(f"the connector's {job.command} reported an error{said}", 502)
        return stats

    def _one(self, job: ConnectorJob, want: str, key: str, values: list[str]) -> dict:
        """The one message of type ``want`` a command answers with (its ``key``)."""
        got: list[dict] = []

        def on_message(kind: str, msg: dict) -> None:
            if kind == want:
                if not isinstance(msg.get(key), dict):
                    raise ConnectorError(f"a {want} message without its {key}", 502)
                got.append(msg[key])
            elif kind in ("RECORD", "STATE"):
                raise ConnectorError(f"the connector's {job.command} printed a {kind}", 502)
        self._exec(job, on_message, values)
        if len(got) != 1:
            raise ConnectorError(f"the connector's {job.command} printed {len(got)} {want} messages, not one", 502)
        return got[0]

    def _draft(self, actor: str, body: Any) -> dict:
        """What an action runs for: a saved sync of the caller's, or an image and a config checked as a save's."""
        self._configured()
        body = body if isinstance(body, dict) else {}
        if body.get("sync"):
            s = self._mine(actor, str(body["sync"]))
            return {"name": s["name"], "image": self._image(s["image"]), "config": s["config"],
                    "secret_scope": s["secret_scope"], "egress": self._egress(s["egress"])}
        config, scope = self._config({} if body.get("config") is None else body["config"], body.get("secret_scope"))
        return {"name": None, "image": self._image(body.get("image")), "config": config, "secret_scope": scope,
                "egress": self._egress(body.get("egress") or [])}

    def _action(self, actor: str, body: Any, command: str, want: str, key: str) -> dict:
        d = self._draft(actor, body)
        self._need_runtime()
        values: list[str] = []
        try:
            files = {}
            if command != "spec":
                config, values = self._resolve(actor, d["secret_scope"], d["config"])
                files["config.json"] = json.dumps(config).encode()
            job = self._job(d["image"], command, files, [] if command == "spec" else d["egress"], self.action_timeout_s)
            out = self.pool.submit(self._one, job, want, key, values).result()
        except ConnectorError as e:
            self._audit(actor, f"connector.{command}_failed", d["name"] or "-", {"image": d["image"]})
            raise ConnectorError(redact(str(e), values), e.status)
        self._audit(actor, f"connector.{command}", d["name"] or "-", {"image": d["image"]})
        return redact_all(out, values)

    def spec(self, actor: str, body: Any) -> dict:
        """The image's ``spec``: what its config takes (run with no config and no network)."""
        return {"spec": self._action(actor, body, "spec", "SPEC", "spec")}

    def check(self, actor: str, body: Any) -> dict:
        """The image's ``check`` of the config, with the caller's secrets: ``{"status", "message"}``."""
        out = self._action(actor, body, "check", "CONNECTION_STATUS", "connectionStatus")
        return {"status": str(out.get("status") or "FAILED"), "message": str(out.get("message") or "")[:2000]}

    def discover(self, actor: str, body: Any) -> dict:
        """The image's ``discover``: the streams it offers, with their keys and sync modes."""
        return {"catalog": self._action(actor, body, "discover", "CATALOG", "catalog")}

    # ---- running a sync -------------------------------------------------------------------------------
    def run_now(self, actor: str, name: str, wait: bool = False) -> dict:
        self._configured()
        s = self._mine(actor, name)
        self._need_runtime()
        if not self._claim(s):
            raise ConnectorError(f"{name} is running", 409)
        fut = self.pool.submit(self._run, s)
        return fut.result() if wait else {"sync": name, "status": "running"}

    def _claim(self, s: dict, next_run_ms: Optional[int] = None) -> bool:
        values: dict[str, Any] = {"running": True}
        if next_run_ms is not None:
            values["next_run_ms"] = next_run_ms
        cond = [connector_syncs.c.id == s["id"], connector_syncs.c.running.is_(False)]
        if next_run_ms is not None:
            cond.append(connector_syncs.c.next_run_ms == s["next_run_ms"])
        with self.engine.begin() as conn:
            return bool(conn.execute(update(connector_syncs).where(and_(*cond)).values(**values)).rowcount)

    def configured_catalog(self, streams: list[dict], catalog: dict) -> tuple[dict, list[dict]]:
        """The catalog the ``read`` gets (the sync's streams as the connector's ``discover`` describes them), and
        the sync's streams with their keys settled."""
        offered: dict[str, list[dict]] = {}
        for st in catalog.get("streams") or []:
            if isinstance(st, dict) and isinstance(st.get("name"), str):
                offered.setdefault(st["name"], []).append(st)
        configured, settled = [], []
        for s in streams:
            found = [st for st in offered.get(s["name"], []) if "namespace" not in s or st.get("namespace") == s["namespace"]]
            if len(found) != 1:
                raise ConnectorError(f"the connector offers {len(found)} streams named {s['name']}" +
                                     ("; give the stream's namespace" if len(found) > 1 else ""), 502)
            st = found[0]
            modes = st.get("supported_sync_modes") or ["full_refresh"]
            mode = s.get("sync_mode") or ("incremental" if s["mode"] != "replace" and "incremental" in modes
                                          else "full_refresh")
            if mode not in modes:
                raise ConnectorError(f"the stream {s['name']} cannot be read {mode}", 502)
            key = s.get("primary_key")
            if key is None:
                paths = st.get("source_defined_primary_key") or []
                if any(not isinstance(p, list) or len(p) != 1 for p in paths):
                    raise ConnectorError(f"the stream {s['name']}'s key is inside an object; name top-level fields "
                                         "in the sync's primary_key", 502)
                key = [p[0] for p in paths]
            if s["mode"] == "upsert" and not key:
                raise ConnectorError(f"the stream {s['name']} has no primary key to upsert by; give primary_key", 502)
            cursor = s.get("cursor_field") or st.get("default_cursor_field") or []
            configured.append({"stream": st, "sync_mode": mode, "cursor_field": cursor,
                               "primary_key": [[k] for k in key],
                               "destination_sync_mode": {"append": "append", "upsert": "append_dedup",
                                                         "replace": "overwrite"}[s["mode"]]})
            settled.append({**s, "primary_key": key})
        return {"streams": configured}, settled

    def _run(self, s: dict) -> dict:
        error, values = None, []
        stats: dict[str, int] = {}
        landing: Optional[_Landing] = None
        try:
            self._configured()
            image, egress = self._image(s["image"]), self._egress(s["egress"])    # the operator's lists, as they are now
            self._need_runtime()
            self._need_write(s["owner"], s["streams"])                            # the owner may since have lost the right
            config, values = self._resolve(s["owner"], s["secret_scope"], s["config"])
            cfg = json.dumps(config).encode()
            limits = s.get("limits") or {}
            catalog = self._one(self._job(image, "discover", {"config.json": cfg}, egress, self.action_timeout_s),
                                "CATALOG", "catalog", values)
            configured, streams = self.configured_catalog(s["streams"], catalog)
            try:
                state = json.loads(s["state"]) if s.get("state") else None
            except ValueError:
                raise ConnectorError("the sync's saved state cannot be read; save the sync's streams again to start over")
            landing = _Landing(self, s, streams, state, min(self.max_records, limits.get("max_records") or self.max_records))
            files = {"config.json": cfg, "catalog.json": json.dumps(configured).encode()}
            if state_file(state) is not None:
                files["state.json"] = state_file(state)

            def on_message(kind: str, msg: dict) -> None:
                if kind == "RECORD":
                    landing.record(msg.get("record"))
                elif kind == "STATE":
                    landing.state_message(msg.get("state"))
            self._exec(self._job(image, "read", files, egress, min(self.timeout_s, limits.get("timeout_s") or self.timeout_s)),
                       on_message, values, stats)
            landing.flush(end=True)
        except Exception as e:  # noqa: BLE001 - recorded on the sync and told, never raised into the scheduler
            error = redact(str(e) if isinstance(e, ConnectorError) else f"{type(e).__name__}: {e}", values)[:1000]
            logger.warning("connector sync %s failed: %s", s["name"], error)
        rows = landing.rows if landing is not None else 0
        status = "failed" if error else "ok"
        counts = {k: int(v) for k, v in stats.items()}
        with self.engine.begin() as conn:
            conn.execute(update(connector_syncs).where(connector_syncs.c.id == s["id"]).values(
                running=False, last_run_ms=_now(), last_status=status, last_rows=rows, last_error=error,
                last_stats=counts or None))
        self._audit(s["owner"], f"connector.sync_{'failed' if error else 'run'}", s["name"],
                    {"image": s["image"], "rows": rows, **counts, **({"error": error} if error else {})})
        if landing is not None and landing.touched and self.changes is not None:
            self.changes.record(sorted(self._urn(t) for t in landing.touched), "connector", f"connector:{s['name']}")
        if error and self.notify is not None:
            try:
                self.notify("saas.sync_failed", f"Connector sync {s['name']} failed", error, [s["owner"]],
                            {"sync": s["name"]})
            except Exception:  # noqa: BLE001 - the failure is recorded either way
                logger.exception("could not tell about connector sync %s", s["name"])
        return {"sync": s["name"], "status": status, "rows": rows, "error": error, "stats": counts}

    def run_due(self, now: Optional[int] = None, wait: bool = False) -> int:
        """Starts every enabled sync whose time has come (the scheduler's leader calls this each tick)."""
        if not self.images:
            return 0
        now = now or _now()
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(connector_syncs).where(and_(
                connector_syncs.c.enabled, connector_syncs.c.next_run_ms.is_not(None),
                connector_syncs.c.next_run_ms <= now)))]
        futures = []
        for s in due:
            if self._claim(s, _next(s["cron"], now)):
                futures.append(self.pool.submit(self._run, s))
        if wait:
            for f in futures:
                f.result()
        return len(futures)

    # ---- landing --------------------------------------------------------------------------------------
    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        from cairn.pipelines.iceberg_v3 import enable

        enable()                                         # pyiceberg writes v3 tables too
        return RestCatalog("cairn-connectors", **self.catalog_props)

    def _commit(self, stream: dict, records: list[dict]) -> int:
        """One batch of a stream's records into its target, in one commit: appended; upserted by ``_key`` (the
        batch's keys deleted, the batch appended); or, for a replaced table, everything deleted and the batch
        appended. The table is created on the first commit; a field not seen before adds a column. Returns
        the rows written."""
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError
        from pyiceberg.expressions import AlwaysTrue, In
        from pyiceberg.types import LongType, NestedField, StringType

        from cairn.pipelines.pipes import fit

        mode, target = stream["mode"], stream["target"]
        names: dict[str, str] = {}                       # column -> the record's field
        for rec in records:
            for i, f in enumerate(rec):
                c = column(f, i)
                if c in (KEY, SYNCED) or names.setdefault(c, f) != f:
                    raise ConnectorError(f"the stream {stream['name']}: two fields would be the column {c} "
                                         f"({KEY} and {SYNCED} are the sync's own)", 502)
        if mode == "upsert":
            latest: dict[str, dict] = {}                 # a key twice in a batch: its last record
            for rec in records:
                parts = [rec.get(k) for k in stream["primary_key"]]
                if any(p is None for p in parts):
                    raise ConnectorError(f"the stream {stream['name']}: a record without its primary key", 502)
                latest[text(parts[0]) if len(parts) == 1 else json.dumps(parts, default=str)] = rec
            keys, records = list(latest), list(latest.values())
        cols: dict[str, Any] = {}
        if mode == "upsert":
            cols[KEY] = pa.array(keys, pa.string())
        for c, f in names.items():
            cols[c] = pa.array([text(rec.get(f)) for rec in records], pa.string())
        cols[SYNCED] = pa.array([_now()] * len(records), pa.int64())
        data = pa.table(cols)
        cat = self._catalog()
        ns, name = target.split(".")
        made = False
        try:
            table = cat.load_table((ns, name))
        except NoSuchTableError:
            if not records:                              # nothing read, nothing to replace
                return 0
            made = True
            try:
                cat.create_namespace(ns)
            except Exception:  # noqa: BLE001 - it exists
                pass
            from pyiceberg.schema import Schema

            fields = [NestedField(i, c, LongType() if c == SYNCED else StringType(), required=c == KEY)
                      for i, c in enumerate(data.column_names, start=1)]
            table = cat.create_table((ns, name), schema=Schema(*fields, identifier_field_ids=[1] if mode == "upsert" else []))
        have = set(table.schema().column_names)
        if mode == "upsert" and KEY not in have:
            raise ConnectorError(f"{target} was not made by an upsert (it has no {KEY}); load into a new table", 409)
        new = [c for c in data.column_names if c not in have]
        if new:                                          # a field not seen before: a column added to the table
            with table.update_schema() as u:
                for c in new:
                    u.add_column(c, LongType() if c == SYNCED else StringType())
            table = cat.load_table((ns, name))
        rows = fit(table, data, target)
        with table.transaction() as tx:
            if mode == "replace" and not made:
                tx.delete(AlwaysTrue())
            elif mode == "upsert" and records and not made:
                tx.delete(In(KEY, keys))
            if records:
                tx.append(rows)
        return len(records)

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, f"connector:{name}", details)


def build_connectors_router(cs: ConnectorSyncs, current_principal) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/connector-syncs", tags=["connectors"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except ConnectorError as e:
            raise HTTPException(e.status, str(e))

    def sync(body: SyncBody) -> dict:
        return body.model_dump(exclude_none=True)

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: cs.list(p.urn))

    @router.post("", status_code=201)
    async def create(body: SyncBody, p=Depends(current_principal)):
        return await call(lambda: cs.save(p.urn, sync(body)))

    @router.post("/spec")
    async def spec(body: DraftBody, p=Depends(current_principal)):
        return await call(lambda: cs.spec(p.urn, body.model_dump(exclude_none=True)))

    @router.post("/check")
    async def check(body: DraftBody, p=Depends(current_principal)):
        return await call(lambda: cs.check(p.urn, body.model_dump(exclude_none=True)))

    @router.post("/discover")
    async def discover(body: DraftBody, p=Depends(current_principal)):
        return await call(lambda: cs.discover(p.urn, body.model_dump(exclude_none=True)))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: cs.get(p.urn, name))

    @router.put("/{name}")
    async def change(name: str, body: SyncBody, p=Depends(current_principal)):
        return await call(lambda: cs.save(p.urn, sync(body), name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: cs.delete(p.urn, name))

    @router.post("/{name}/run", status_code=202)
    async def run(name: str, wait: bool = False, p=Depends(current_principal)):
        return await call(lambda: cs.run_now(p.urn, name, wait))

    return router
