"""OTLP destinations the bundled OpenTelemetry Collector exports to (the customer's own backend: a Collector,
Grafana Cloud, Datadog, Honeycomb, New Relic, ...), as configuration (kind ``observability_exporters``).

An **exporter** names a protocol (``grpc`` or ``http``, OTLP either way), an endpoint, headers whose values come
from Cairn secret scopes (``{"name": "authorization", "scope": "otel", "key": "token", "prefix": "Bearer "}``), TLS
(plaintext only when asked for, a CA file the Collector can read, a server name), which signals (traces, metrics,
logs) and a trace sampling ratio.

**Rendering.** :func:`render` turns the enabled exporters into a Collector configuration fragment that is loaded
next to the bundled one (``--config`` twice; the Collector merges them): an exporter per destination and a pipeline
per destination and signal, from the OTLP receiver through **the same redaction processors as the bundled
pipelines** (``attributes/credentials``, ``redaction/values`` and, for traces, ``transform/urls``), then a sampler
when asked, then the batch. Header values are never in the file: each is ``${env:CAIRN_EXPORTER_<ID>_<N>}``, an
environment variable of the Collector (a Compose env file, or a Kubernetes Secret).

**Fail closed.** :func:`check_redaction` refuses any configuration in which a pipeline reaches an exporter without
the redaction processors; :func:`render` checks its own output with it, the Helm chart checks the fragment it is
given the same way, and the bundled configuration passes it (a test keeps it so).

**Applying.** ``apply`` (``POST /v1/observability/exporters/apply``) reads each header's value from its secret scope
as the person applying (their access to the scope is checked and audited as ``secret.read``) and writes
``exporters.yaml`` and ``exporters.env`` into ``CAIRN_OBS_EXPORTERS_DIR`` (the directory the Compose overlay mounts
into the Collector); the Collector takes them when it is recreated (``docker compose up -d cairn-otel-collector``).
On Kubernetes the chart takes the fragment as ``cairn.observability.exportersConfig`` and the values from the Secret
``cairn.observability.exportersSecret`` (``GET .../exporters/collector-config`` gives both the fragment and the
variable names the Secret needs).
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, model_validator

ID = r"^[a-z0-9][a-z0-9\-]{1,40}$"
SIGNALS = ("traces", "metrics", "logs")
# The bundled configuration's redaction processors (deploy/observability/stack/otel-collector.yaml).
REDACTION = {"traces": ("attributes/credentials", "transform/urls", "redaction/values"),
             "metrics": ("attributes/credentials", "redaction/values"),
             "logs": ("attributes/credentials", "transform/urls", "redaction/values")}
HEADER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FORBIDDEN_HEADERS = {"host", "content-type", "content-length", "content-encoding", "te", "connection"}


class ExporterError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class Header(BaseModel):
    name: str = Field(pattern=HEADER.pattern, description="the header, e.g. authorization, x-honeycomb-team, api-key")
    scope: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", description="the secret scope holding its value")
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", description="the secret's key in that scope")
    prefix: str = Field("", max_length=32, pattern=r"^[A-Za-z0-9 _-]*$",
                        description='written before the value, e.g. "Bearer " or "Basic "')


class Tls(BaseModel):
    insecure: bool = Field(False, description="plaintext (no TLS): only for a Collector on a private network")
    ca_file: str = Field("", max_length=512, pattern=r"^(/[A-Za-z0-9._/-]+)?$",
                         description="a CA bundle file inside the Collector's container")
    server_name: str = Field("", max_length=253, pattern=r"^[A-Za-z0-9.-]*$")


class Exporter(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field("", max_length=200)
    protocol: Literal["grpc", "http"] = Field("http", description="OTLP over gRPC or HTTP (protobuf)")
    endpoint: str = Field(min_length=3, max_length=1024,
                          description="http: https://host[:port][/path] (the /v1/<signal> paths are added); "
                                      "grpc: host:port or https://host:port")
    headers: list[Header] = Field(default_factory=list, max_length=10)
    signals: list[Literal["traces", "metrics", "logs"]] = Field(default_factory=lambda: list(SIGNALS), min_length=1)
    sampling_ratio: float = Field(1.0, gt=0, le=1, description="the share of traces sent (1: all)")
    tls: Tls = Field(default_factory=Tls)
    enabled: bool = True

    @model_validator(mode="after")
    def _shape(self):
        if len(set(self.signals)) != len(self.signals):
            raise ValueError("signals: each of traces, metrics, logs at most once")
        names = [h.name.lower() for h in self.headers]
        if len(set(names)) != len(names):
            raise ValueError("headers: each header once")
        bad = [h for h in names if h in FORBIDDEN_HEADERS]
        if bad:
            raise ValueError(f"headers: {', '.join(bad)} is set by the Collector")
        e = self.endpoint.strip()
        if re.search(r"\s|@|\$\{", e):
            raise ValueError("endpoint: no spaces, credentials or ${...} in it (headers carry credentials)")
        if self.protocol == "http":
            u = urlsplit(e)
            if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment:
                raise ValueError("endpoint: http(s)://host[:port][/path] for OTLP over HTTP")
            if u.scheme == "http" and not self.tls.insecure:
                raise ValueError("endpoint: http:// is plaintext; use https://, or set tls.insecure for a Collector "
                                 "on a private network")
            if u.scheme == "https" and self.tls.insecure:
                raise ValueError("tls.insecure with an https:// endpoint: say one or the other")
        else:
            hostport = e.split("://", 1)[1] if e.startswith("https://") else e
            if "://" in hostport or not re.match(r"^[A-Za-z0-9.-]+:\d{1,5}$", hostport):
                raise ValueError("endpoint: host:port (or https://host:port) for OTLP over gRPC")
            if e.startswith("https://") and self.tls.insecure:
                raise ValueError("tls.insecure with an https:// endpoint: say one or the other")
        return self


def validate(body: dict) -> dict:
    try:
        m = Exporter.model_validate(body)
    except Exception as e:  # pydantic's ValidationError
        from cairn.observability.service import _why

        raise ExporterError(_why(e))
    return m.model_dump(mode="json")


def env_name(eid: str, i: int) -> str:
    return f"CAIRN_EXPORTER_{re.sub(r'[^A-Z0-9]', '_', eid.upper())}_{i}"


def _exporter(doc: dict) -> tuple[str, dict]:
    name = ("otlp_grpc/" if doc["protocol"] == "grpc" else "otlp_http/") + doc["id"]
    headers = {h["name"]: f"{h.get('prefix') or ''}${{env:{env_name(doc['id'], i)}}}"
               for i, h in enumerate(doc.get("headers") or [])}
    tls = doc.get("tls") or {}
    t: dict[str, Any] = {}
    if tls.get("insecure"):
        t["insecure"] = True
    if tls.get("ca_file"):
        t["ca_file"] = tls["ca_file"]
    if tls.get("server_name"):
        t["server_name_override"] = tls["server_name"]
    out: dict[str, Any] = {"endpoint": doc["endpoint"].strip().rstrip("/")}
    if headers:
        out["headers"] = headers
    if t:
        out["tls"] = t
    out["sending_queue"] = {"enabled": True, "queue_size": 2000}
    out["retry_on_failure"] = {"enabled": True, "max_elapsed_time": "300s"}
    return name, out


def render(exporters: list[dict]) -> dict:
    """The Collector configuration fragment for the enabled exporters (checked: every pipeline redacts first)."""
    cfg: dict[str, Any] = {"exporters": {}, "processors": {}, "service": {"pipelines": {}}}
    for doc in sorted(exporters, key=lambda d: d["id"]):
        if not doc.get("enabled", True):
            continue
        name, ex = _exporter(doc)
        cfg["exporters"][name] = ex
        for sig in doc.get("signals") or SIGNALS:
            procs = ["memory_limiter", *REDACTION[sig]]
            if sig == "traces" and float(doc.get("sampling_ratio", 1.0)) < 1.0:
                sampler = f"probabilistic_sampler/{doc['id']}"
                cfg["processors"][sampler] = {"sampling_percentage": round(float(doc["sampling_ratio"]) * 100, 4)}
                procs.append(sampler)
            procs.append("batch")
            cfg["service"]["pipelines"][f"{sig}/export-{doc['id']}"] = {
                "receivers": ["otlp"], "processors": procs, "exporters": [name]}
    if not cfg["processors"]:
        del cfg["processors"]
    check_redaction(cfg)
    return cfg


def check_redaction(cfg: dict) -> None:
    """Refuses a Collector configuration (or fragment) in which a pipeline exports without redacting first."""
    pipelines = ((cfg or {}).get("service") or {}).get("pipelines") or {}
    for pid, p in pipelines.items():
        sig = str(pid).split("/", 1)[0]
        if sig not in REDACTION:
            raise ExporterError(f"pipeline {pid}: not a traces, metrics or logs pipeline")
        if not (p or {}).get("exporters"):
            continue
        procs = list((p or {}).get("processors") or [])
        missing = [x for x in REDACTION[sig] if x not in procs]
        if missing:
            raise ExporterError(f"pipeline {pid} exports without the redaction processors {', '.join(missing)}: "
                                "refused (every exporter gets redacted telemetry)")
        if "batch" in procs and any(procs.index(x) > procs.index("batch") for x in REDACTION[sig]):
            raise ExporterError(f"pipeline {pid}: redaction must come before the batch")


def merge(base: dict, extra: dict) -> dict:
    """What the Collector does with two --config files: maps merge, everything else is replaced."""
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def to_yaml(cfg: dict) -> str:
    head = ("# Rendered by Cairn from its OTLP destinations (Observability -> Export; kind observability_exporters).\n"
            "# Loaded next to the bundled otel-collector.yaml. Do not edit: apply again from Cairn.\n")
    return head + (yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False) if cfg.get("exporters") else "{}\n")


def secret_env(exporters: list[dict], read: Callable[[str, str], str]) -> dict[str, str]:
    """The Collector's environment for the header values: ``read(scope, key)`` reads one (as the person applying)."""
    out: dict[str, str] = {}
    for doc in exporters:
        if not doc.get("enabled", True):
            continue
        for i, h in enumerate(doc.get("headers") or []):
            v = read(h["scope"], h["key"])
            if not isinstance(v, str) or not v or re.search(r"[\r\n\x00]", v):
                raise ExporterError(f"exporter {doc['id']}: the secret {h['scope']}/{h['key']} is empty or not one "
                                    "line", 422)
            out[env_name(doc["id"], i)] = v
    return out


def write(directory: str, cfg: dict, env: dict[str, str]) -> list[str]:
    """Writes the fragment and its env file (0600) atomically into ``directory``."""
    if not directory:
        raise ExporterError("the Collector's exporters directory is not configured (CAIRN_OBS_EXPORTERS_DIR): on "
                            "Kubernetes give the chart exportersConfig and exportersSecret instead", 503)
    os.makedirs(directory, exist_ok=True)
    written = []
    for name, text, mode in (("exporters.yaml", to_yaml(cfg), 0o644),
                             ("exporters.env", "".join(f"{k}={v}\n" for k, v in sorted(env.items())), 0o600)):
        path = os.path.join(directory, name)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        written.append(path)
    return written


def bundled_config(path: Optional[str] = None) -> dict:
    """The bundled Collector configuration shipped with Cairn (for checks)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in [path] if path else [os.path.join(here, "..", "..", "..", "deploy", "observability", "stack",
                                               "otel-collector.yaml"), "/etc/cairn/otel-collector.yaml"]:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return yaml.safe_load(fh)
    raise ExporterError("the bundled Collector configuration is not in this installation", 503)
