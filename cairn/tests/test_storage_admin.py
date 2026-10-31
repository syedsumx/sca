"""Cairn Store administration from the control plane: the SigV4 signer, the client, and the /storage API."""

import datetime as dt
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cairn.storage.store_admin import StoreAdmin, StoreAdminError, sigv4_headers


def test_signer_matches_botocore():
    pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = "http://cairn-store:8333/-/admin/v1/audit?after=5&limit=10&format=jsonl"
    body = b'{"max_bytes": 1}'
    req = AWSRequest(method="PUT", url=url, data=body)
    S3SigV4Auth(Credentials("AK", "SECRET"), "s3", "us-east-1").add_auth(req)
    when = dt.datetime.strptime(req.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    ours = sigv4_headers("PUT", url, body, "AK", "SECRET", now=when)
    assert ours["authorization"] == req.headers["Authorization"]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def store():
    """A real cairn-store binary (CAIRN_STORE_BIN, or a release build in cairn/store/target)."""
    candidates = [os.environ.get("CAIRN_STORE_BIN", ""),
                  str(Path(os.environ.get("CARGO_TARGET_DIR", Path(__file__).parents[1] / "store/target")) / "release/cairn-store")]
    binary = next((c for c in candidates if c and os.path.exists(c)), None)
    if not binary:
        pytest.skip("no cairn-store binary (set CAIRN_STORE_BIN)")
    d = tempfile.mkdtemp()
    port = _free_port()
    env = {**os.environ, "CAIRN_STORE_ACCESS_KEY": "admin", "CAIRN_STORE_SECRET_KEY": "admin-secret-key", "CAIRN_STORE_DATA": d,
           "CAIRN_STORE_ADDR": f"127.0.0.1:{port}", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_BUCKETS": "lake",
           "CAIRN_STORE_LOG_LEVEL": "warn"}
    proc = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield StoreAdmin(f"http://127.0.0.1:{port}", "admin", "admin-secret-key")
    proc.terminate()
    proc.wait(10)


def test_client_against_a_real_store(store):
    assert [b["name"] for b in store.buckets()] == ["lake"]
    b = store.set_quota("lake", 10 << 30, None)
    assert b["quota"] == {"max_bytes": 10 << 30} and b["usage"] == {"bytes": 0, "objects": 0}
    assert store.delete_quota("lake")["quota"] is None
    entries = store.audit()
    assert [e["action"] for e in entries][-2:] == ["quota.put", "quota.delete"]
    v = store.verify_audit()
    assert v["valid"] and v["entries_checked"] == len(entries)
    with pytest.raises(StoreAdminError) as ei:
        store.set_quota("no-such-bucket", 1, None)
    assert ei.value.status == 404
    bad = StoreAdmin(store.url, "admin", "wrong-secret")
    with pytest.raises(StoreAdminError) as ei:
        bad.stats()
    assert ei.value.status == 403


def test_storage_api_is_for_admins(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.common.security import Principal
    from cairn.storage.store_api import build_router

    who = {"urn": "urn:li:corpuser:admin"}
    recorded = []

    class Audit:
        def record(self, *a):
            recorded.append(a)

    app = FastAPI()
    app.include_router(build_router(store, lambda: Principal(urn=who["urn"], via="dev"), lambda urn: urn.endswith(":admin"), Audit()))
    c = TestClient(app)
    assert c.get("/storage/v1/buckets").json()[0]["name"] == "lake"
    assert c.put("/storage/v1/buckets/lake/quota", json={"max_objects": 100}).json()["quota"] == {"max_objects": 100}
    assert c.put("/storage/v1/buckets/lake/quota", json={}).status_code == 400
    assert recorded[-1][:4] == ("storage", "quota.set", "urn:li:corpuser:admin", "lake")
    assert c.get("/storage/v1/audit/verify").json()["valid"] is True
    who["urn"] = "urn:li:corpuser:analyst"
    assert c.get("/storage/v1/buckets").status_code == 403
    assert json.loads(c.get("/storage/v1/stats").text)["detail"].startswith("storage administration")
