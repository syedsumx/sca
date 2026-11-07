"""SIEM export against a real receiver: Vector (timberio/vector) listening as a Splunk HTTP Event Collector,
a syslog server over TCP and over TLS, and an HTTPS-style JSON endpoint, writing what it accepts to files.
The audit log arrives in order, complete and verifiable; a refused token or a receiver that is down holds the
cursor (nothing lost, nothing sent twice once it recovers); a rewind sends again. Needs docker."""

import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time

import httpx
import pytest

from cairn.common.audit import AuditLog, verify_entries
from cairn.common.db import get_engine, init_db

IMAGE = os.environ.get("CAIRN_VECTOR_IMAGE", "timberio/vector:0.43.1-alpine")
HEC_TOKEN = "hec-6f1c2b8e-test-token"


def _docker() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason=f"needs docker and {IMAGE}")


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CONFIG = """
sources:
  hec: {type: splunk_hec, address: "0.0.0.0:8088", valid_tokens: ["%(token)s"]}
  syslog_tcp: {type: syslog, mode: tcp, address: "0.0.0.0:5514"}
  syslog_tls:
    type: syslog
    mode: tcp
    address: "0.0.0.0:6514"
    tls: {enabled: true, crt_file: /certs/server.pem, key_file: /certs/server.key}
  http: {type: http_server, address: "0.0.0.0:8080", decoding: {codec: json}, headers: ["Authorization"]}
sinks:
  hec_out: {type: file, inputs: [hec], path: /out/hec.log, encoding: {codec: json}}
  tcp_out: {type: file, inputs: [syslog_tcp], path: /out/tcp.log, encoding: {codec: json}}
  tls_out: {type: file, inputs: [syslog_tls], path: /out/tls.log, encoding: {codec: json}}
  http_out: {type: file, inputs: [http], path: /out/http.log, encoding: {codec: json}}
"""


@pytest.fixture(scope="module")
def vector():
    d = tempfile.mkdtemp(prefix="siem-")
    os.makedirs(f"{d}/certs")
    os.makedirs(f"{d}/out")
    os.chmod(f"{d}/out", 0o777)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", f"{d}/certs/server.key",
                    "-out", f"{d}/certs/server.pem"], check=True, capture_output=True)
    os.chmod(f"{d}/certs/server.key", 0o644)
    with open(f"{d}/vector.yaml", "w") as f:
        f.write(CONFIG % {"token": HEC_TOKEN})
    ports = {"hec": port(), "tcp": port(), "tls": port(), "http": port()}
    name = f"cairn-siem-{os.getpid()}"

    def start():
        subprocess.run(["docker", "run", "-d", "--name", name, "-v", f"{d}/vector.yaml:/etc/vector/vector.yaml:ro",
                        "-v", f"{d}/certs:/certs:ro", "-v", f"{d}/out:/out",
                        "-p", f"127.0.0.1:{ports['hec']}:8088", "-p", f"127.0.0.1:{ports['tcp']}:5514",
                        "-p", f"127.0.0.1:{ports['tls']}:6514", "-p", f"127.0.0.1:{ports['http']}:8080",
                        IMAGE, "--config", "/etc/vector/vector.yaml"], check=True, capture_output=True)
        # Docker's port proxy accepts connections before Vector listens, so wait for real answers from each source.
        end = time.time() + 60
        while time.time() < end:
            try:
                httpx.get(f"http://127.0.0.1:{ports['hec']}/services/collector/health", timeout=2)
                httpx.get(f"http://127.0.0.1:{ports['http']}/", timeout=2)
                ctx = ssl.create_default_context(cafile=f"{d}/certs/server.pem")
                with socket.create_connection(("127.0.0.1", ports["tls"]), timeout=2) as raw, \
                        ctx.wrap_socket(raw, server_hostname="localhost"):
                    pass
                with socket.create_connection(("127.0.0.1", ports["tcp"]), timeout=1):
                    return
            except (OSError, httpx.HTTPError):
                time.sleep(0.5)
        raise RuntimeError(subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-2000:])

    def stop():
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    start()
    yield {"dir": d, "ports": ports, "start": start, "stop": stop, "name": name}
    stop()
    shutil.rmtree(d, ignore_errors=True)


def received(v, which: str, n: int, seconds: int = 30) -> list[dict]:
    path = f"{v['dir']}/out/{which}.log"
    end = time.time() + seconds
    rows: list = []
    while time.time() < end:
        if os.path.exists(path):
            with open(path) as f:
                rows = [json.loads(line) for line in f if line.strip()]
            if len(rows) >= n:
                return rows
        time.sleep(0.5)
    return rows


def test_the_audit_log_reaches_each_sink_in_order_and_verifiable(vector, tmp_path):
    from cairn.common.siem import SiemExport, load_sinks

    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    audit = AuditLog(engine)
    for i in range(25):
        audit.record("access" if i % 5 else "security", "access.denied" if i % 7 == 0 else "policy.changed",
                     f"urn:li:corpuser:u{i % 3}", f"urn:li:dataset:(urn:li:dataPlatform:iceberg,t{i},PROD)",
                     {"i": i, "note": 'quotes " and ] and = and | here'})
    tok = tmp_path / "hec"
    tok.write_text(HEC_TOKEN + "\n")
    bearer = tmp_path / "bearer"
    bearer.write_text("http-bearer-token")
    p = vector["ports"]
    cfg = {"sinks": [
        {"name": "splunk", "type": "splunk_hec", "url": f"http://127.0.0.1:{p['hec']}", "token_file": str(tok),
         "index": "security", "batch": 10},
        {"name": "syslog", "type": "syslog", "host": "127.0.0.1", "port": p["tcp"], "batch": 10},
        {"name": "qradar", "type": "syslog", "host": "localhost", "port": p["tls"], "tls": True, "format": "cef",
         "ca_file": f"{vector['dir']}/certs/server.pem", "categories": ["security"]},
        {"name": "lake", "type": "http", "url": f"http://127.0.0.1:{p['http']}/", "token_file": str(bearer)}]}
    sinks = load_sinks(json.dumps(cfg))
    with pytest.raises(ValueError, match="token_file"):
        load_sinks(json.dumps({"sinks": [{"name": "x", "type": "splunk_hec", "url": "https://x"}]}))
    siem = SiemExport(engine, audit, sinks, hostname="cairn-test")
    out = siem.ship()
    assert out["splunk"] == {"sent": 25, "seq": 25} and out["syslog"]["sent"] == 25, out
    assert out["qradar"]["sent"] == 5 and out["qradar"]["seq"] == 25 and out["lake"]["sent"] == 25

    hec = received(vector, "hec", 25)
    assert [e["seq"] for e in hec] == list(range(1, 26)) and hec[0]["splunk_index"] == "security"
    # What the SIEM holds proves itself: the chain verifies from the entries as received.
    chain = [{k: e[k] for k in ("seq", "id", "ts", "category", "action", "actor", "subject", "details", "prev_hash", "hash")}
             for e in hec]
    assert verify_entries(chain)["valid"] and verify_entries(chain)["head"] == audit.verify_chain()["head"]

    tcp = received(vector, "tcp", 25)
    assert len(tcp) == 25 and {e["appname"] for e in tcp} == {"cairn"} and tcp[0]["hostname"] == "cairn-test"
    assert [json.loads(e["message"])["seq"] for e in tcp] == list(range(1, 26))
    assert tcp[0]["severity"] == "warning" and tcp[1]["severity"] == "info" and tcp[0]["facility"] == "audit"
    assert tcp[3]["cairn@32473"]["category"] == "access" and "hash" in tcp[3]["cairn@32473"]

    tls = received(vector, "tls", 5)
    assert len(tls) == 5 and all(e["message"].startswith("CEF:0|Cairn|Cairn|") for e in tls)
    assert "suser=urn:li:corpuser:u0" in tls[0]["message"] and "cs1=security" in tls[0]["message"]
    assert "note\\\": \\\"quotes" in tls[0]["message"] or "quotes" in tls[0]["message"]

    http = received(vector, "http", 25)
    assert [e["seq"] for e in http] == list(range(1, 26)) and http[0]["Authorization"] == "Bearer http-bearer-token"
    assert siem.ship() == {"splunk": {"sent": 0, "seq": 25}, "syslog": {"sent": 0, "seq": 25},
                           "qradar": {"sent": 0, "seq": 25}, "lake": {"sent": 0, "seq": 25}}   # nothing new

    # A refused token holds the cursor and backs off; once fixed, the rest arrives, nothing twice.
    tok.write_text("wrong-token")
    audit.record("security", "key.rotated", "urn:li:corpuser:ops", None, {"n": 1})
    out = siem.ship()
    assert "401" in out["splunk"]["error"] or "403" in out["splunk"]["error"], out
    st = {s["sink"]: s for s in siem.status()}
    assert st["splunk"]["seq"] == 25 and st["splunk"]["lag"] == 1 and st["splunk"]["failures"] == 1
    assert siem.ship()["splunk"] == {"waiting": True}                       # backing off
    tok.write_text(HEC_TOKEN)
    with engine.begin() as conn:                                             # the backoff has passed
        from sqlalchemy import update

        from cairn.common.siem import siem_cursors
        conn.execute(update(siem_cursors).values(retry_ms=None))
    assert siem.ship()["splunk"] == {"sent": 1, "seq": 26}
    hec = received(vector, "hec", 26)
    assert [e["seq"] for e in hec] == list(range(1, 27))

    # The receiver goes down: held; it comes back: delivered.
    vector["stop"]()
    audit.record("security", "user.suspended", "urn:li:corpuser:ops", "urn:li:corpuser:mallory", {})
    assert "error" in siem.ship()["syslog"]
    assert {s["sink"]: s for s in siem.status()}["syslog"]["seq"] == 26
    vector["start"]()
    with engine.begin() as conn:
        conn.execute(update(siem_cursors).values(retry_ms=None))
    assert siem.ship()["syslog"] == {"sent": 1, "seq": 27}

    # Rewound: sent again from where it says (a SIEM that lost its index).
    siem.rewind("urn:li:corpuser:ops", "lake", 20)
    assert siem.ship()["lake"]["sent"] == 8                                 # 21..28 (the rewind is audited too)
    assert any(e["action"] == "siem.rewound" for e in audit.entries("security", limit=5))
