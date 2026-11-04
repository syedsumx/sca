"""SIEM export: Cairn's audit log pushed to a security team's SIEM as it is written.

Sinks are listed in ``CAIRN_SIEM_SINKS`` (JSON, or a file holding it)::

    {"sinks": [
      {"name": "splunk", "type": "splunk_hec", "url": "https://splunk:8088", "token_file": "/run/secrets/hec",
       "index": "security", "sourcetype": "cairn:audit"},
      {"name": "qradar", "type": "syslog", "host": "qradar.corp", "port": 6514, "tls": true, "format": "cef",
       "ca_file": "/etc/ssl/corp-ca.pem"},
      {"name": "sentinel", "type": "http", "url": "https://collector/ingest", "token_file": "/run/secrets/http"}
    ]}

* ``splunk_hec``: Splunk's HTTP Event Collector (``/services/collector/event``, ``Authorization: Splunk
  <token>``), one event per entry with its time, ``index``, ``source`` (``cairn``) and ``sourcetype``.
* ``syslog``: RFC 5424 messages over TCP (RFC 6587 octet counting), with TLS when ``tls`` (RFC 5425;
  ``ca_file`` to trust a private CA), facility *log audit* (13). ``format``: ``json`` (the entry as JSON,
  default) or ``cef`` (ArcSight Common Event Format, for QRadar, ArcSight and Sentinel's CEF connector).
* ``http``: the entries as a JSON array, ``POST``ed with ``Authorization: Bearer <token>`` when a token
  is given (Elastic, Datadog, Sumo Logic, a log pipeline).

Tokens come from ``token_file`` (a mounted secret), never from the configuration itself. ``categories``
limits a sink to some audit categories.

**Delivery** is at least once and in order: the notifier sends each sink the entries after the last one it
acknowledged, in batches (``batch``, default 500), and moves the sink's cursor only when the batch is
accepted. Each entry carries its ``id``, ``seq`` and chain ``hash``, so a SIEM can drop repeats and check
nothing is missing. A sink that fails is retried with backoff (up to five minutes) and shows its error and
lag; one notifier at a time ships a sink (a lease). ``GET /v1/audit/siem`` shows each sink;
``POST /v1/audit/siem/<sink>/rewind`` sends again from a ``seq``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

TYPES = ("splunk_hec", "syslog", "http")
FACILITY_AUDIT = 13
PEN = 32473                          # the example private enterprise number, for the structured data id

siem_cursors = Table(
    "cairn_siem_cursors", metadata,
    Column("sink", String(64), primary_key=True),
    Column("seq", BigInteger, nullable=False, default=0),         # the last entry the sink accepted
    Column("sent", BigInteger, nullable=False, default=0),
    Column("last_ms", BigInteger, nullable=True),
    Column("failures", Integer, nullable=False, default=0),
    Column("retry_ms", BigInteger, nullable=True),
    Column("error", String(2000), nullable=True),
    Column("lease_owner", String(64), nullable=True),
    Column("lease_until", BigInteger, nullable=True),
)


class SiemError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def load_sinks(raw: Optional[str] = None) -> list[dict]:
    raw = (raw if raw is not None else os.environ.get("CAIRN_SIEM_SINKS", "")).strip()
    if not raw:
        return []
    cfg = json.loads(open(raw).read() if not raw.startswith("{") else raw)
    sinks = cfg.get("sinks") if isinstance(cfg, dict) else None
    if not isinstance(sinks, list):
        raise ValueError('CAIRN_SIEM_SINKS: {"sinks": [...]}')
    seen = set()
    for s in sinks:
        name, kind = s.get("name"), s.get("type")
        if not isinstance(name, str) or not name or len(name) > 64 or name in seen:
            raise ValueError("CAIRN_SIEM_SINKS: each sink has a unique name")
        seen.add(name)
        if kind not in TYPES:
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: type is one of {', '.join(TYPES)}")
        if kind in ("splunk_hec", "http") and not str(s.get("url") or "").startswith(("https://", "http://")):
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: url")
        if kind == "splunk_hec" and not s.get("token_file"):
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: token_file (the HEC token)")
        if kind == "syslog" and not (s.get("host") and isinstance(s.get("port"), int)):
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: host and port")
        if kind == "syslog" and s.get("format", "json") not in ("json", "cef"):
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: format is json or cef")
        if s.get("token_file") and not os.path.isfile(s["token_file"]):
            raise ValueError(f"CAIRN_SIEM_SINKS: {name}: token_file {s['token_file']} is not readable")
    return sinks


def _token(sink: dict) -> Optional[str]:
    if not sink.get("token_file"):
        return None
    with open(sink["token_file"]) as f:
        return f.read().strip()


def _event(e: dict) -> dict:
    return {k: e.get(k) for k in ("seq", "id", "ts", "category", "action", "actor", "subject", "details",
                                  "prev_hash", "hash")}


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# --------------------------------------------------------------------------- formats
def _sd(value: Any) -> str:
    """An RFC 5424 structured-data parameter value: ``"``, ``\\`` and ``]`` escaped."""
    return str(value if value is not None else "").replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _cef(value: Any, header: bool = False) -> str:
    text = str(value if value is not None else "")
    if header:
        return text.replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\n", "\\n").replace("\r", "\\r")


def cef(e: dict, version: str = "1") -> str:
    sev = 7 if any(w in e["action"] for w in ("denied", "refused", "revoked", "failed", "deleted")) else 3
    ext = {"rt": int(_epoch(e["ts"]) * 1000), "suser": e.get("actor"), "externalId": e.get("id"),
           "cs1Label": "category", "cs1": e.get("category"), "cs2Label": "subject", "cs2": e.get("subject"),
           "cs3Label": "hash", "cs3": e.get("hash"), "cn1Label": "seq", "cn1": e.get("seq"),
           "msg": json.dumps(e.get("details"), sort_keys=True, default=str)}
    return (f"CEF:0|Cairn|Cairn|{_cef(version, True)}|{_cef(e['action'], True)}|{_cef(e['action'], True)}|{sev}|"
            + " ".join(f"{k}={_cef(v)}" for k, v in ext.items() if v is not None))


def syslog_message(e: dict, fmt: str, hostname: str) -> bytes:
    severity = 4 if any(w in e["action"] for w in ("denied", "refused", "failed")) else 6
    pri = FACILITY_AUDIT * 8 + severity
    sd = (f'[cairn@{PEN} seq="{_sd(e.get("seq"))}" category="{_sd(e.get("category"))}" '
          f'actor="{_sd(e.get("actor"))}" hash="{_sd(e.get("hash"))}"]')
    body = cef(e) if fmt == "cef" else json.dumps(_event(e), sort_keys=True, default=str)
    msgid = "".join(ch for ch in e["action"] if 33 <= ord(ch) <= 126)[:32] or "-"
    line = f"<{pri}>1 {e['ts']} {hostname} cairn - {msgid} {sd} {body}"
    data = line.encode()
    return f"{len(data)} ".encode() + data                         # RFC 6587 octet counting


# --------------------------------------------------------------------------- the shipper
class SiemExport:
    def __init__(self, engine, audit, sinks: list[dict], http=None, hostname: Optional[str] = None,
                 version: str = "1"):
        self.engine, self.audit, self.sinks = engine, audit, {s["name"]: s for s in sinks}
        self.http = http
        self.hostname = hostname or socket.gethostname()
        self.version = version
        self.me = uuid.uuid4().hex
        with self.engine.begin() as conn:
            have = {r.sink for r in conn.execute(select(siem_cursors.c.sink))}
            for name in self.sinks:
                if name not in have:
                    conn.execute(insert(siem_cursors).values(sink=name, seq=0, sent=0, failures=0))

    def _send(self, sink: dict, batch: list[dict]) -> None:
        kind = sink["type"]
        if kind == "syslog":
            return self._syslog(sink, batch)
        verify: Any = sink.get("ca_file") or sink.get("verify", True)
        token = _token(sink)
        if kind == "splunk_hec":
            body = "".join(json.dumps({"time": _epoch(e["ts"]), "host": self.hostname, "source": "cairn",
                                       "sourcetype": sink.get("sourcetype", "cairn:audit"),
                                       **({"index": sink["index"]} if sink.get("index") else {}),
                                       "event": _event(e)}, sort_keys=True, default=str) for e in batch)
            url = sink["url"].rstrip("/") + "/services/collector/event"
            headers = {"Authorization": f"Splunk {token}", "Content-Type": "application/json"}
        else:
            body = json.dumps([_event(e) for e in batch], sort_keys=True, default=str)
            url = sink["url"]
            headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        import httpx

        client = self.http or httpx.Client(timeout=30, verify=verify)
        try:
            r = client.post(url, content=body.encode(), headers=headers)
        finally:
            if self.http is None:
                client.close()
        if r.status_code >= 300:
            raise SiemError(f"{sink['name']} answered {r.status_code}: {r.text[:200]}")
        if kind == "splunk_hec":
            try:
                code = r.json().get("code", 0)
            except ValueError:
                code = 0
            if code not in (0, None):
                raise SiemError(f"{sink['name']}: HEC code {code}: {r.text[:200]}")

    def _syslog(self, sink: dict, batch: list[dict]) -> None:
        raw = socket.create_connection((sink["host"], int(sink["port"])), timeout=30)
        try:
            conn: Any = raw
            if sink.get("tls"):
                ctx = ssl.create_default_context(cafile=sink.get("ca_file"))
                if sink.get("verify") is False:
                    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                conn = ctx.wrap_socket(raw, server_hostname=sink.get("server_name") or sink["host"])
            conn.sendall(b"".join(syslog_message(e, sink.get("format", "json"), self.hostname) for e in batch))
            # Syslog has no acknowledgement: end the stream cleanly and wait for the receiver to close, so
            # every byte was read before the cursor moves.
            conn.settimeout(10)
            try:
                if sink.get("tls"):
                    conn = conn.unwrap()
                conn.shutdown(socket.SHUT_WR)
                while conn.recv(4096):
                    pass
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                raw.close()

    def _lease(self, name: str, now: int) -> bool:
        with self.engine.begin() as conn:
            r = conn.execute(update(siem_cursors).where(
                siem_cursors.c.sink == name,
                (siem_cursors.c.lease_until.is_(None)) | (siem_cursors.c.lease_until < now)
                | (siem_cursors.c.lease_owner == self.me)).values(lease_owner=self.me, lease_until=now + 120_000))
            return r.rowcount == 1

    def _release(self, name: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(siem_cursors).where(siem_cursors.c.sink == name, siem_cursors.c.lease_owner == self.me)
                         .values(lease_owner=None, lease_until=None))

    def ship(self, max_batches: int = 20) -> dict:
        """Sends each sink what it has not accepted yet; the notifier calls this every tick."""
        from cairn.common import metrics

        out = {}
        now = _now()
        for name, sink in self.sinks.items():
            with self.engine.connect() as conn:
                cur = conn.execute(select(siem_cursors).where(siem_cursors.c.sink == name)).first()
            if cur.retry_ms and now < cur.retry_ms:
                out[name] = {"waiting": True}
                continue
            if not self._lease(name, now):
                out[name] = {"elsewhere": True}
                continue
            sent, seq = 0, cur.seq
            try:
                cats = set(sink.get("categories") or [])
                size = int(sink.get("batch", 500))
                for _ in range(max_batches):
                    rows = []
                    for e in self.audit.export(after_seq=seq, batch=size):
                        rows.append(e)
                        if len(rows) >= size:
                            break
                    if not rows:
                        break
                    batch = [e for e in rows if not cats or e["category"] in cats]
                    if batch:
                        self._send(sink, batch)
                    seq, sent = rows[-1]["seq"], sent + len(batch)
                    with self.engine.begin() as conn:      # accepted: the cursor moves past it
                        conn.execute(update(siem_cursors).where(siem_cursors.c.sink == name).values(
                            seq=seq, sent=siem_cursors.c.sent + len(batch), last_ms=_now(), failures=0,
                            retry_ms=None, error=None))
                    try:
                        metrics.SIEM_SENT.labels(name).inc(len(batch))
                    except Exception:  # noqa: BLE001 - metrics never stop delivery
                        pass
                    if len(rows) < size:
                        break
                out[name] = {"sent": sent, "seq": seq}
            except Exception as e:  # noqa: BLE001 - recorded, retried with backoff
                failures = (cur.failures or 0) + 1
                delay = min(300_000, 2 ** min(failures, 8) * 1000)
                msg = str(e).splitlines()[0][:2000] if str(e) else type(e).__name__
                logger.warning("SIEM sink %s: %s (retry in %ds)", name, msg, delay // 1000)
                with self.engine.begin() as conn:
                    conn.execute(update(siem_cursors).where(siem_cursors.c.sink == name).values(
                        failures=failures, retry_ms=_now() + delay, error=msg))
                out[name] = {"sent": sent, "seq": seq, "error": msg}
            finally:
                self._release(name)
        return out

    # ---- operating it ------------------------------------------------------------------------
    def status(self) -> list[dict]:
        from cairn.common.audit import audit_log
        from sqlalchemy import func

        with self.engine.connect() as conn:
            head = conn.execute(select(func.max(audit_log.c.seq))).scalar() or 0
            rows = {r.sink: r for r in conn.execute(select(siem_cursors))}
        out = []
        for name, sink in self.sinks.items():
            r = rows.get(name)
            out.append({"sink": name, "type": sink["type"], "seq": r.seq if r else 0, "lag": head - (r.seq if r else 0),
                        "sent": r.sent if r else 0, "last_ms": r.last_ms if r else None,
                        "failures": r.failures if r else 0, "error": r.error if r else None,
                        "retry_ms": r.retry_ms if r else None})
        return out

    def rewind(self, actor: str, name: str, seq: int) -> dict:
        if name not in self.sinks:
            raise SiemError("no such sink", 404)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise SiemError("seq: the last entry the sink should be taken to have (0: everything)")
        with self.engine.begin() as conn:
            conn.execute(update(siem_cursors).where(siem_cursors.c.sink == name).values(
                seq=seq, retry_ms=None, failures=0, error=None))
        if self.audit is not None:
            self.audit.record("security", "siem.rewound", actor, None, {"sink": name, "seq": seq})
        return {"sink": name, "seq": seq}


def build_siem_router(siem: SiemExport, current_principal, authorize: Callable) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/audit/siem", tags=["governance"])

    @router.get("")
    def status(p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return siem.status()

    @router.post("/{name}/rewind")
    def rewind(name: str, seq: int = Body(..., embed=True), p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        try:
            return siem.rewind(p.urn, name, seq)
        except SiemError as e:
            raise HTTPException(e.status, str(e))

    return router
