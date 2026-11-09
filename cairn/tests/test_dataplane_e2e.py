"""Split control plane and data plane (L135), with real components on separate Docker networks.

The control plane (Cairn's API and gateway, its PDP) runs here. The data plane is the customer's: Trino and its
storage (Cairn Store) on an internal network nothing outside can reach, and the connector, which is also on an
outside network and connects only out, to the gateway. The control plane cannot reach the data plane's Trino at all.

* A governed query from the hosted API runs on the data plane's Trino through the tunnel, Trino's access-control
  questions answered by the hosted PDP (masks and row filters as for hosted Trino).
* A job runs on the data plane (a container there), reading through the data plane's Trino as its owner.
* The control plane gets a presigned URL for one object, never the storage key.
* Failures fail closed: the tunnel down refuses queries and jobs; a forged connector (never enrolled, or signing as an
  enrolled one with another key) and a revoked one are refused, and Trino refuses queries without a decision.

Needs docker and the trinodb/trino, cairn/store and python:3.12-slim images; pip downloads httpx and cryptography for
the connector's Python 3.12 (a bare python image)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

TRINO = os.environ.get("CAIRN_TRINO_IMAGE", "trinodb/trino:484")
STORE = os.environ.get("CAIRN_STORE_IMAGE", "cairn/store:0.1.0")
PY = os.environ.get("CAIRN_DATAPLANE_TEST_PY_IMAGE", "python:3.12-slim")
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
KEY = "dataplane-e2e-" + "k" * 24
STORE_KEYS = ("customer-store-key", "customer-store-secret-never-in-cairn")
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:hive,sales.orders,PROD)"
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
BOB = {"X-Cairn-Actor": "urn:li:corpuser:bob"}
_TV = int(TRINO.rsplit(":", 1)[-1]) if TRINO.rsplit(":", 1)[-1].isdigit() else 484


def _ready() -> bool:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return False
    return all(subprocess.run(["docker", "image", "inspect", i], capture_output=True, timeout=30).returncode == 0
               for i in (TRINO, STORE, PY))


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker and {TRINO}, {STORE}, {PY}")


def docker(*args, check=True, timeout=300):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr[-2000:]}")
    return r.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(p: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"nothing on {p}")


def ip_on(container: str, network: str) -> str:
    return docker("inspect", "-f", "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}", container)


@pytest.fixture(scope="module")
def deps(tmp_path_factory):
    """httpx and cryptography for Python 3.12, for the connector in a bare python image."""
    d = tmp_path_factory.mktemp("py312")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(d), "--python-version", "3.12",
                        "--only-binary=:all:", "--platform", "manylinux2014_x86_64", "--implementation", "cp",
                        "httpx==0.28.1", "cryptography"], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return str(d)


@pytest.fixture(scope="module")
def cp(tmp_path_factory):
    """The control plane: Cairn's API and gateway (threads here), its PDP, Trino access on."""
    import uvicorn

    from cairn.app import create_api_app, create_gateway_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cp")
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY, "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "hive", "database": ""}}),
           "CAIRN_MANAGED_COMPUTE": "false", "CAIRN_DATAPLANE_POLL_SECONDS": "2"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret", dev_trust_actor_header=True,
                            gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=True)
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    s.dataplanes.offline_ms = 4000                 # quick to notice a tunnel that went down (default 30 s)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{u}"}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("admin")),
        Policy(id="sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="jobs", effect="allow", actions=["RUN_JOBS"], condition=sub("bob")),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": "urn:li:corpuser:admin"}),
        Policy(id="bob-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub("bob")),
    ]))
    from cairn.governance.workspaces import WorkspaceIn
    from cairn.quality.collectors import ConnectionRegistry

    s.workspaces.put(WorkspaceIn(id="sales", name="Sales", domains=["urn:li:domain:sales"], members=["urn:li:corpuser:bob"]),
                     "urn:li:corpuser:admin", True)
    s.data_access.registry = ConnectionRegistry({"acme-trino": {
        "url": "trino://cairn@trino:8080/lake?dataplane=acme", "run_as_user": True,
        "catalog": {"platform": "hive", "database": "", "dialect": "trino"}}})
    api_port, gw_port = free_port(), free_port()
    servers = []
    for app, p in ((create_api_app(s), api_port), (create_gateway_app(s), gw_port)):
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        wait_port(p)
    yield {"api": f"http://127.0.0.1:{api_port}", "gw_port": gw_port, "s": s, "db": f"{tmp}/cairn.db"}
    for server in servers:
        server.should_exit = True


@pytest.fixture(scope="module")
def dp(cp, deps, tmp_path_factory):
    """The data plane: an internal network (Trino, the store), the connector also on an outside one."""
    tag = uuid.uuid4().hex[:6]
    inner, outer = f"cairn-dp-in-{tag}", f"cairn-dp-out-{tag}"
    store, trino, conn = f"dp-store-{tag}", f"dp-trino-{tag}", f"dp-connector-{tag}"
    d = tmp_path_factory.mktemp("dp")
    api = cp["api"]
    created = []
    try:
        # Internal, and no address of the host on it: nothing here (the control plane) can reach it.
        docker("network", "create", "--internal", "-o", "com.docker.network.bridge.inhibit_ipv4=true", inner)
        created.append(("network", inner))
        docker("network", "create", outer)
        created.append(("network", outer))
        gw_ip = docker("network", "inspect", "-f", "{{(index .IPAM.Config 0).Gateway}}", outer)
        # The customer's storage: keys only here (an env file docker reads, never a command line).
        (d / "store.env").write_text(f"CAIRN_STORE_ACCESS_KEY={STORE_KEYS[0]}\nCAIRN_STORE_SECRET_KEY={STORE_KEYS[1]}\n"
                                     "CAIRN_STORE_BUCKETS=lakehouse\nCAIRN_STORE_FSYNC=false\nCAIRN_STORE_META=\n")
        docker("run", "-d", "--name", store, "--network", outer, "--env-file", str(d / "store.env"),
               "--tmpfs", "/data:rw,size=256m,uid=10001,gid=10001", STORE)
        created.append(("container", store))
        docker("network", "connect", "--alias", "store", inner, store)
        # The data plane in Cairn, its enrolment token, and the connector.
        r = httpx.post(f"{api}/v1/dataplanes", json={"name": "acme", "description": "Acme's cluster",
                                                     "connection": "acme-trino"}, headers=ADMIN)
        assert r.status_code == 201, r.text
        token = httpx.post(f"{api}/v1/dataplanes/acme/enrollment-tokens", headers=ADMIN).json()["token"]
        for name, value in (("token", token), ("access", STORE_KEYS[0]), ("secret", STORE_KEYS[1])):
            (d / name).write_text(value)
            os.chmod(d / name, 0o600)
        os.makedirs(d / "state", exist_ok=True)
        docker("run", "-d", "--name", conn, "--network", outer, "--network-alias", "connector",
               "-v", f"{deps}:/opt/deps:ro", "-v", f"{SRC}:/opt/src:ro", "-v", f"{d}:/run/dp",
               "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-e", "PYTHONPATH=/opt/deps:/opt/src", "-e", f"CAIRN_DATAPLANE_URL=http://{gw_ip}:{cp['gw_port']}",
               "-e", "CAIRN_DATAPLANE_STATE_DIR=/run/dp/state", "-e", "CAIRN_DATAPLANE_ENROLLMENT_TOKEN_FILE=/run/dp/token",
               "-e", "CAIRN_DATAPLANE_POLL_SECONDS=2", "-e", "CAIRN_DATAPLANE_TRINO_URL=http://trino:8080",
               "-e", "CAIRN_DATAPLANE_OPA_LISTEN=0.0.0.0:8181", "-e", "CAIRN_DATAPLANE_JOBS_RUNTIME=docker",
               "-e", f"CAIRN_DATAPLANE_JOBS_IMAGE={PY}", "-e", f"CAIRN_DATAPLANE_JOBS_NETWORK={inner}",
               "-e", "CAIRN_DATAPLANE_JOBS_MEMORY_MB=256", "-e", "CAIRN_DATAPLANE_JOBS_TRINO_URL=http://trino:8080",
               "-e", "CAIRN_DATAPLANE_STORAGE_ENDPOINT=http://" + ip_on(store, outer) + ":8333",
               "-e", "CAIRN_DATAPLANE_STORAGE_ACCESS_KEY_FILE=/run/dp/access",
               "-e", "CAIRN_DATAPLANE_STORAGE_SECRET_KEY_FILE=/run/dp/secret", "-e", "CAIRN_DATAPLANE_STORAGE_BUCKETS=lakehouse",
               PY, "python", "-m", "cairn.dataplane.connector")
        created.append(("container", conn))
        docker("network", "connect", "--alias", "connector", inner, conn)
        end = time.time() + 60
        while httpx.get(f"{api}/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] != "up":
            assert time.time() < end, docker("logs", conn, check=False) + \
                subprocess.run(["docker", "logs", conn], capture_output=True, text=True).stderr
            time.sleep(0.5)
        # The data plane's Trino: only on the internal network; its access control asks the connector.
        os.makedirs(d / "catalog")
        s3 = ["fs.s3.enabled=true" if _TV >= 484 else "fs.native-s3.enabled=true", "s3.endpoint=http://store:8333",
              "s3.region=us-east-1", "s3.path-style-access=true",
              "s3.aws-access-key=${ENV:STORE_ACCESS_KEY}", "s3.aws-secret-key=${ENV:STORE_SECRET_KEY}"]
        (d / "catalog" / "lake.properties").write_text("\n".join(
            ["connector.name=hive", "hive.metastore=file", "hive.metastore.catalog.dir=s3://lakehouse/meta", *s3, ""]))
        (d / "access-control.properties").write_text("\n".join([
            "access-control.name=opa", "opa.policy.uri=http://connector:8181/allow",
            "opa.policy.batched-uri=http://connector:8181/batch", "opa.policy.row-filters-uri=http://connector:8181/row-filters",
            "opa.policy.column-masking-uri=http://connector:8181/column-mask", ""]))
        (d / "trino.env").write_text(f"STORE_ACCESS_KEY={STORE_KEYS[0]}\nSTORE_SECRET_KEY={STORE_KEYS[1]}\n")
        os.chmod(d, 0o755)
        os.chmod(d / "catalog", 0o755)
        for f in ("catalog/lake.properties", "access-control.properties"):
            os.chmod(d / f, 0o644)
        docker("run", "-d", "--name", trino, "--network", inner, "--network-alias", "trino", "--env-file", str(d / "trino.env"),
               "-v", f"{d}/catalog:/etc/trino/catalog:ro",
               "-v", f"{d}/access-control.properties:/etc/trino/access-control.properties:ro", TRINO)
        created.append(("container", trino))

        def sql(q: str, user: str = "admin"):
            return subprocess.run(["docker", "exec", trino, "trino", "--user", user, "--output-format", "JSON",
                                   "--execute", q], capture_output=True, text=True, timeout=180)
        end = time.time() + 240
        while True:
            r = sql("SELECT 1")
            if r.returncode == 0:
                break
            assert time.time() < end, r.stderr[-2000:] + docker("logs", "--tail", "50", trino, check=False)
            time.sleep(3)
        # The customer's data, written by the data plane's Trino into the customer's storage.
        for q in ("CREATE SCHEMA lake.sales",
                  "CREATE TABLE lake.sales.orders (id integer, email varchar, region varchar)",
                  "INSERT INTO lake.sales.orders VALUES (1, 'ann@acme.com', 'EU'), (2, 'raj@example.org', 'US'), "
                  "(3, 'li@acme.com', 'EU')"):
            r = sql(q)
            assert r.returncode == 0, r.stderr[-2000:]
        yield {"inner": inner, "outer": outer, "store": store, "trino": trino, "conn": conn, "dir": d, "sql": sql,
               "store_ip": ip_on(store, outer)}
    finally:
        for kind, name in reversed(created):
            docker("rm" if kind == "container" else "network", *(["-f"] if kind == "container" else []), name,
                   check=False)
        for c in docker("ps", "-aq", "--filter", "label=app.kubernetes.io/part-of=cairn", "--filter",
                        "name=cairn-dp-job-", check=False).split():
            docker("rm", "-f", c, check=False)


def statement(api: str, sql: str, headers: dict, **body) -> httpx.Response:
    return httpx.post(f"{api}/v1/statements", json={"sql": sql, **body}, headers=headers, timeout=180)


def read_all(api: str, first: dict, headers: dict) -> list:
    rows, doc = list(first.get("rows") or []), first
    while doc.get("next"):
        r = httpx.post(f"{api}/v1/statements/next", json={"cursor": doc["next"]}, headers=headers, timeout=120)
        assert r.status_code == 200, r.text
        doc = r.json()
        rows += doc.get("rows") or []
    return rows


def test_the_control_plane_cannot_reach_the_data_plane(dp):
    trino_ip = ip_on(dp["trino"], dp["inner"])
    with pytest.raises(OSError):
        socket.create_connection((trino_ip, 8080), 3).close()
    assert "8181" not in docker("port", dp["conn"], check=False)         # the connector publishes nothing


def test_governed_query_runs_on_the_data_plane(cp, dp):
    api, s = cp["api"], cp["s"]
    asked = []
    real = s.dataplanes.opa

    def recording(c, kind, doc):
        asked.append(kind)
        return real(c, kind, doc)
    s.dataplanes.opa = recording
    try:
        # The workspace computes on the data plane: the statement goes through its tunnel.
        r = httpx.put(f"{api}/v1/compute/workspaces/sales", json={"compute": "customer", "dataplane": "acme"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["dataplane"] == "acme", r.text
        r = statement(api, "SELECT id, email, region FROM sales.orders ORDER BY id", BOB, workspace="sales")
        assert r.status_code == 200, r.text
        rows = read_all(api, r.json(), BOB)
    finally:
        s.dataplanes.opa = real
    assert [x[0] for x in rows] == [1, 3], rows                         # bob's row filter (region = 'EU')
    assert all(x[2] == "EU" for x in rows) and all(x[1] != "ann@acme.com" and "@" not in str(x[1])[:1] for x in rows), rows
    assert rows[0][1] != "ann@acme.com"                                 # masked (PII)
    # Trino asked the hosted PDP, through the connector, for this query: access, then the row filter and the mask.
    assert {"allow", "row-filters"} <= set(asked) or {"batch", "row-filters"} <= set(asked), asked
    assert "column-mask" in asked or "batch" in asked, asked
    # The admin sees the rows as they are.
    r = statement(api, "SELECT id, email FROM sales.orders ORDER BY id", ADMIN, connection="acme-trino")
    assert r.status_code == 200, r.text
    assert read_all(api, r.json(), ADMIN)[0] == [1, "ann@acme.com"]
    # Managed: the workspace's statements no longer go to the data plane (and this Cairn has no managed pool).
    r = httpx.put(f"{api}/v1/compute/workspaces/sales", json={"compute": "managed"}, headers=ADMIN)
    assert r.status_code == 422 and "managed compute pool" in r.text
    httpx.put(f"{api}/v1/compute/workspaces/sales", json={"compute": "customer", "dataplane": "acme"}, headers=ADMIN)


def test_job_runs_on_the_data_plane(cp, dp):
    s = cp["s"]
    code = ("import json, os, socket, time, urllib.request\n"
            "base = os.environ['CAIRN_TRINO_URL']\n"
            "h = {'X-Trino-User': os.environ['CAIRN_USER'], 'X-Trino-Catalog': 'lake', 'X-Trino-Schema': 'sales'}\n"
            "doc = json.load(urllib.request.urlopen(urllib.request.Request(base + '/v1/statement', "
            "data=b'SELECT count(*) FROM orders', headers=h)))\n"
            "rows = []\n"
            "while doc.get('nextUri'):\n"
            "    rows += doc.get('data') or []\n"
            "    doc = json.load(urllib.request.urlopen(urllib.request.Request(doc['nextUri'], headers=h)))\n"
            "rows += doc.get('data') or []\n"
            "print(json.dumps({'host': socket.gethostname(), 'trino': base, 'rows': rows, "
            "'token': bool(os.environ.get('CAIRN_TRINO_TOKEN')), 'error': (doc.get('error') or {}).get('message')}))\n")
    s.jobs.save("urn:li:corpuser:bob", {"name": "dp-count", "kind": "python", "code": code})
    r = s.jobs.run_now("urn:li:corpuser:bob", "dp-count", wait=True)
    assert r["status"] == "succeeded", r
    out = json.loads(s.jobs.run_detail("urn:li:corpuser:bob", "dp-count", r["run"])["output"].strip().splitlines()[-1])
    assert out["trino"] == "http://trino:8080" and out["token"] is True, out
    assert out["rows"] == [[2]], out                                     # as bob: only the EU rows, decided by Cairn


def test_scoped_storage_urls_not_keys(cp, dp):
    api = cp["api"]
    # The customer writes a report into its storage (with its own key, here).
    from cairn.dataplane.connector import presign

    put = presign(f"http://{dp['store_ip']}:8333", "us-east-1", *STORE_KEYS, "PUT", "lakehouse", "reports/q3.csv", 60)
    with httpx.Client(trust_env=False) as h:
        assert h.put(put, content=b"region,total\nEU,7\n").status_code == 200
        r = httpx.post(f"{api}/v1/dataplanes/acme/storage/presign", json={"bucket": "lakehouse", "key": "reports/q3.csv",
                                                                          "ttl_seconds": 60}, headers=ADMIN, timeout=60)
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert STORE_KEYS[1] not in url and "X-Amz-Expires=60" in url
        assert h.get(url).content == b"region,total\nEU,7\n"                     # that object, that method
        assert h.get(url.replace("reports/q3.csv", "reports/q4.csv")).status_code == 403
        assert h.put(url, content=b"x").status_code == 403
    r = httpx.post(f"{api}/v1/dataplanes/acme/storage/presign", json={"bucket": "other", "key": "a"}, headers=ADMIN, timeout=60)
    assert r.status_code == 502 and "not one Cairn may be handed" in r.text
    r = httpx.post(f"{api}/v1/dataplanes/acme/storage/presign", json={"bucket": "lakehouse", "key": "a", "ttl_seconds": 86400},
                   headers=ADMIN, timeout=60)
    assert "X-Amz-Expires=900" in r.json()["url"]                                    # capped
    # The storage key was never in the control plane: not in its database, not in its memory of the calls.
    assert STORE_KEYS[1].encode() not in open(cp["db"], "rb").read()


def test_forged_connectors_are_refused(cp, dp):
    from cairn.agent import crypto

    gw = f"http://127.0.0.1:{cp['gw_port']}"
    real = httpx.get(f"{cp['api']}/v1/dataplanes/acme/connectors", headers=ADMIN).json()[0]["connector_id"]
    forged = crypto.Identity.generate()
    for cid in ("dpc_0000000000000000", real):         # never enrolled; enrolled, but signed with another key
        for rel, body in ((f"/dataplanes/tunnel/{cid}/poll", {}),
                          (f"/dataplanes/tunnel/{cid}/opa/allow", {"input": {"action": {"operation": "ExecuteQuery"},
                                                                             "context": {"identity": {"user": "admin"}}}})):
            raw = json.dumps(body).encode()
            h = crypto.sign_request(forged, cid, "POST", rel, raw)
            r = httpx.post(gw + rel, content=raw, headers={**h, "content-type": "application/json"})
            assert r.status_code == 401, (cid, rel, r.text)
    r = httpx.post(gw + "/dataplanes/tunnel/acme/poll", json={})
    assert r.status_code == 401 and "not enrolled" in r.text                         # no signature at all
    ident = crypto.Identity.generate()
    r = httpx.post(gw + "/dataplanes/tunnel/enroll", json={
        "token": "cairn_dpe_forged-token-xxxxxxxx", "signing_public": ident.signing_public,
        "encryption_public": ident.encryption_public, "hostname": "evil", "version": "1"})
    assert r.status_code == 401


def test_tunnel_down_fails_closed_then_recovers(cp, dp):
    api, s = cp["api"], cp["s"]
    docker("stop", "-t", "2", dp["conn"])
    end = time.time() + 30
    while httpx.get(f"{api}/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] != "down":
        assert time.time() < end
        time.sleep(0.5)
    r = statement(api, "SELECT id FROM sales.orders", BOB, workspace="sales")
    assert r.status_code == 503 and "tunnel to data plane acme is down" in r.text, r.text
    r = s.jobs.run_now("urn:li:corpuser:bob", "dp-count", wait=True)
    assert r["status"] == "failed" and "tunnel to data plane acme is down" in r["error"], r
    # The data plane's Trino has no decision either: its queries are refused.
    out = dp["sql"]("SELECT count(*) FROM lake.sales.orders", user="bob")
    assert out.returncode != 0, out.stdout
    docker("start", dp["conn"])
    end = time.time() + 60
    while httpx.get(f"{api}/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] != "up":
        assert time.time() < end
        time.sleep(0.5)
    r = statement(api, "SELECT id FROM sales.orders ORDER BY id", BOB, workspace="sales")
    assert r.status_code == 200 and [x[0] for x in read_all(api, r.json(), BOB)] == [1, 3], r.text


def test_a_revoked_connector_is_refused(cp, dp):
    api = cp["api"]
    cid = httpx.get(f"{api}/v1/dataplanes/acme/connectors", headers=ADMIN).json()[0]["connector_id"]
    assert httpx.post(f"{api}/v1/dataplanes/acme/connectors/{cid}/revoke", headers=ADMIN).status_code == 204
    assert httpx.get(f"{api}/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] == "down"
    r = statement(api, "SELECT id FROM sales.orders", BOB, workspace="sales")
    assert r.status_code == 503, r.text
    time.sleep(3)
    logs = subprocess.run(["docker", "logs", dp["conn"]], capture_output=True, text=True).stderr
    assert "this connector was revoked" in logs
    out = dp["sql"]("SELECT count(*) FROM lake.sales.orders", user="admin")    # Trino gets no decision: refused
    assert out.returncode != 0
