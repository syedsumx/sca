"""The fleet (L161), end to end: the vendor's console started with ``cairn fleet serve`` on a real port, its operators
made with ``cairn fleet operator``, and a real install (Cairn's services, with a release feed and a licence) checking
in over HTTP. What needs attention is worked out from what installs say; a check-in that is unsigned, signed with
another or a rotated key, replayed, or from another time is refused; nothing an install sends goes beyond the fixed
list; and the console's own sign-in is all that opens it."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.fleet.reporter import FIELDS, FleetReporter
from cairn.fleet.server import Fleet, clean, sign
from cairn.release import publish as pub
from test_release_feed import keypair, manifest, served  # noqa: F401 - the served fixture

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def console(tmp_path):
    """The fleet console as an operator runs it: its own database, master key and operators file."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    ops = tmp_path / "operators.json"
    r = subprocess.run([sys.executable, "-m", "cairn.cli", "fleet", "operator", "--name", "ops-anna",
                        "--file", str(ops)], capture_output=True, text=True, env=env, check=True)
    token = r.stderr.strip().rsplit(" ", 1)[-1]
    assert token.startswith("cfo_") and oct(ops.stat().st_mode & 0o777) == "0o600"
    assert token not in ops.read_text()                                    # only its digest is kept
    (tmp_path / "master.key").write_text("m" * 48)
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "cairn.cli", "fleet", "serve", "--host", "127.0.0.1",
                             "--port", str(port)], env={**env, "CAIRN_FLEET_DATABASE_URL": f"sqlite:///{tmp_path}/fleet.db",
                                                        "CAIRN_FLEET_MASTER_KEY_FILE": str(tmp_path / "master.key"),
                                                        "CAIRN_FLEET_OPERATORS_FILE": str(ops)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        raise AssertionError(proc.stderr.read().decode())
    yield url, {"Authorization": f"Bearer {token}"}
    proc.terminate()
    proc.wait(10)


def test_installs_check_in_and_the_console_says_what_needs_attention(console, served, tmp_path):  # noqa: F811
    url, op = console
    # The console's own sign-in, and nothing else, opens it.
    assert httpx.get(f"{url}/fleet/v1/installs").status_code == 401
    assert httpx.get(f"{url}/fleet/v1/installs", headers={"Authorization": "Bearer cfo_wrong"}).status_code == 401
    acme = httpx.post(f"{url}/fleet/v1/installs", headers=op,
                      json={"name": "acme-prod", "customer": "Acme Retail", "channel": "stable"}).json()
    quiet = httpx.post(f"{url}/fleet/v1/installs", headers=op, json={"name": "beta-eu", "customer": "Beta GmbH"}).json()
    assert acme["key"] and acme["id"].startswith("inst_")
    assert httpx.post(f"{url}/fleet/v1/installs", headers=op, json={"name": "x", "customer": "y",
                                                                    "secret": 1}).status_code == 400
    again = httpx.post(f"{url}/fleet/v1/installs", headers=op, json={"name": "acme-prod", "customer": "Acme Retail"})
    assert again.status_code == 409 and acme["id"] in again.json()["detail"]     # rotate, never register twice

    # A real install: Cairn's services with a release feed that offers a security release.
    from iac_world import build

    feed_dir, feed_url = served
    signer, keys = keypair("rel-1")
    pub.publish(feed_dir, pub.sign_manifest(manifest("0.2.0", "0.1.0", severity="security"), [signer]), [signer])
    (tmp_path / "fleet.key").write_text(acme["key"])
    dead = f"http://127.0.0.1:{free_port()}/health"
    s = build(tmp_path, env={"CAIRN_RELEASE_FEED": feed_url, "CAIRN_FLEET_URL": url,
                             "CAIRN_FLEET_INSTALL_ID": acme["id"], "CAIRN_FLEET_KEY_FILE": str(tmp_path / "fleet.key"),
                             "CAIRN_FLEET_HEALTH_URLS": json.dumps({"pdp": dead})})
    s.release_feed.keys, s.release_feed.dev_keys = keys, {}
    s.release_feed.check()
    rep = s.fleet_reporter
    assert isinstance(rep, FleetReporter)

    # What leaves the install: the fixed list, and nothing naming data or people.
    body = rep.body()
    assert set(body) == set(FIELDS)
    assert body["updates"]["target"] == "0.2.0" and body["health"]["components"] == {"pdp": "down"}
    assert "Acme" not in json.dumps(body) and "corpuser" not in json.dumps(body)
    first = rep.run_due()
    assert first == "sent", rep.last()
    assert rep.run_due() is None                                          # once per interval

    listed = httpx.get(f"{url}/fleet/v1/installs", headers=op).json()["installs"]
    by = {i["name"]: i for i in listed}
    assert by["acme-prod"]["version"] == "0.1.0"
    assert by["acme-prod"]["attention"] == ["unhealthy", "security_update"]
    assert by["beta-eu"]["attention"] == ["silent"]                         # never checked in
    assert [i["name"] for i in listed][0] == "beta-eu"                      # worst first
    summary = httpx.get(f"{url}/fleet/v1/summary", headers=op).json()
    assert summary["installs"] == 2 and summary["attention"]["silent"] == 1 and summary["versions"]["0.1.0"] == 1
    detail = httpx.get(f"{url}/fleet/v1/installs/{acme['id']}", headers=op).json()
    assert len(detail["history"]) == 1 and detail["history"][0]["updates"]["severity"] == "security"

    # The install's operators see exactly what was sent, and how it went.
    from cairn.app import create_api_app
    from iac_world import ANA_TOKEN, TOKEN

    http = TestClient(create_api_app(s))
    assert http.get("/v1/system/fleet", headers={"Authorization": f"Bearer {ANA_TOKEN}"}).status_code == 403
    mine = http.get("/v1/system/fleet", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert mine["last"]["status"] == "sent" and "security_update" in mine["last"]["detail"]
    assert set(mine["next_body"]) == set(FIELDS)

    # Refused: a replay, another time, an unsigned body, another key, an unknown install (the same answer).
    raw = json.dumps(body, separators=(",", ":")).encode()
    post = lambda b, sig: httpx.post(f"{url}/fleet/v1/checkin", content=b, headers={"X-Cairn-Signature": sig})  # noqa: E731
    assert post(raw, sign(acme["key"], raw)).status_code == 409                              # replay
    late = json.dumps({**body, "sent_ms": int(time.time() * 1000) - 3600_000}).encode()
    assert post(late, sign(acme["key"], late)).status_code == 400
    fresh = json.dumps({**body, "sent_ms": int(time.time() * 1000) + 1000}).encode()
    assert post(fresh, "").status_code == 401
    assert post(fresh, sign(quiet["key"], fresh)).status_code == 401
    ghost = json.dumps({**body, "install": "inst_nobody"}).encode()
    assert post(ghost, sign(acme["key"], ghost)).json()["detail"] == post(fresh, "").json()["detail"]

    big = b'{"install":"' + b"x" * (70 * 1024) + b'"}'
    assert post(big, "sha256=00").status_code == 413                                       # never read whole

    # A rotated key: the old one stops at once, the new one works.
    new = httpx.post(f"{url}/fleet/v1/installs/{acme['id']}/rotate", headers=op).json()["key"]
    assert post(fresh, sign(acme["key"], fresh)).status_code == 401
    assert post(fresh, sign(new, fresh)).status_code == 200
    assert httpx.delete(f"{url}/fleet/v1/installs/{quiet['id']}", headers=op).status_code == 200
    assert [i["name"] for i in httpx.get(f"{url}/fleet/v1/installs", headers=op).json()["installs"]] == ["acme-prod"]


def test_only_the_fixed_list_is_kept(tmp_path):
    import sqlalchemy

    from cairn.common.db import metadata
    from cairn.fleet.common import checkins_table, installs_table

    stored = clean({"install": "x", "version": "2.1.0", "customer_rows": [1, 2], "licence": {
        "id": "lic_1", "customer": "Acme", "status": "valid", "expires_at": "2027-01-01"},
        "updates": {"target": "2.2.0", "last_plan": {"status": "failed", "requested_by": "urn:li:corpuser:ana"}},
        "health": {"components": {"api": "ok", "pdp": {"nested": 1}}}, "secrets": "nope"})
    assert "customer_rows" not in stored and "secrets" not in stored and "customer" not in stored["licence"]
    assert stored["updates"]["last_plan"] == {"status": "failed"} and stored["health"]["components"] == {"api": "ok"}
    e = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/f.db")
    metadata.create_all(e, tables=[installs_table, checkins_table])
    from cairn.fleet.server import FleetError, Operators

    with pytest.raises(FleetError, match="operator token"):                 # no operators file: nobody, not a 500
        Operators(str(tmp_path / "missing.json")).who("Bearer cfo_x")
    with pytest.raises(Exception, match="32 characters"):
        Fleet(e, "short")
    f = Fleet(e, "k" * 40)
    reg = f.register("ops-anna", {"name": "acme-prod", "customer": "Acme"})
    raw = json.dumps({"install": reg["id"], "sent_ms": int(time.time() * 1000), "version": "2.1.0",
                      "updates": {"last_plan": {"status": "rolled_back"}},
                      "licence": {"status": "valid", "expires_at": "2026-12-01", "over_seats": True}}).encode()
    out = f.checkin(raw, sign(reg["key"], raw))
    assert out["attention"] == ["update_failed", "over_seats", "licence_expiring"]
    assert hashlib.sha256(reg["key"].encode()).hexdigest() not in json.dumps(f.get(reg["id"]))
