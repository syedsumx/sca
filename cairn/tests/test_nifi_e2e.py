"""Managed NiFi (L152) end to end on the Docker runtime, with the real Apache NiFi 2.12.0 image (unmodified), Cairn's
real API, Cairn Store with STS, Cairn Catalog deciding every table through Cairn, and Trino:

* nobody but Cairn reaches NiFi: it listens on the host's loopback, over TLS with the deployment's own CA, and takes
  only Cairn's user, whose password is in no environment, command line, log or file in the clear; people open the
  canvas through Cairn, refused without the privilege (and NiFi's own sign-in is not reachable through it);
* a flow is deployed by Cairn through NiFi's REST API, its sensitive parameters from a secret scope read as the
  flow's owner, landing JSON records in a lakehouse table with PutIcebergRecord against Cairn Catalog, signed in as
  the owner with a lakehouse session of theirs (no store key, no superuser); a person without DEPLOY_NIFI_FLOW is
  refused, and so is a sink the owner may not write;
* the table lands governed: a column labelled Confidential comes back masked through Trino for someone else, as it is
  for the owner;
* the flow is a dataFlow in Cairn Core with its owner and domain, its root group a dataJob from the declared source
  to the table, its activity a run (Core's dataProcessInstance and a run in pipeline observability);
* neither secret is in NiFi's flow export, its log or the container's settings;
* deleting the flow removes its group from NiFi and ends its lakehouse session; deleting the deployment removes the
  container and its volumes.

Needs docker, the store and catalog binaries (the Trino fixtures of tests/test_trino_e2e.py) and the apache/nifi
image (CAIRN_NIFI_IMAGE, default apache/nifi:2.12.0, pulled beforehand: it is large; CAIRN_NIFI_VERSION for another
version)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import pytest
from sqlalchemy import select

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, port, trino, wait  # noqa: F401  (fixtures)

VERSION = os.environ.get("CAIRN_NIFI_VERSION", "2.12.0")     # the upgrade matrix tries newer ones
IMAGE = os.environ.get("CAIRN_NIFI_IMAGE", f"apache/nifi:{VERSION}")


def _image_here() -> bool:
    if not _ready():
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60).returncode == 0


pytestmark = pytest.mark.skipif(not _image_here(), reason=f"needs docker, the binaries and {IMAGE} (docker pull it)")

if _ready():
    from test_spark_e2e import SERVICE, STS, people, s_issuer  # noqa: F401  (fixtures)

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
DEP, FLOW = "nfdev", "contacts_to_lake"
TABLE = "urn:li:dataset:(urn:li:dataPlatform:iceberg,crm.contacts,PROD)"
SOURCE = "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.public.contacts,PROD)"
SECRET = "crm-api-token-" + "z" * 24
RUN: dict = {}                          # the run read once the rows landed
RECORDS = [{"id": 1, "name": "Jane", "email": "jane@acme.com"}, {"id": 2, "name": "Bo", "email": "bo@example.org"}]


@pytest.fixture(scope="module")
def nifi_env(tmp_path_factory):
    """Cairn's settings for managed NiFi, before the Cairn API fixture builds its services."""
    d = tmp_path_factory.mktemp("nifipins")
    (d / "pins.json").write_text(json.dumps({"nifi": {"deployments": [VERSION]}}))
    env = {"CAIRN_NIFI_RUNTIME": "docker", "CAIRN_NIFI_DEPLOY_KEY": "nifi-deploy-key-" + "n" * 24,
           "CAIRN_VERSION_PINS": str(d / "pins.json"),
           "CAIRN_NIFI_DOCKER_IMAGES": json.dumps({VERSION: IMAGE}), "CAIRN_NIFI_PROXY_PREFIX": "",
           "CAIRN_NIFI_DOCKER_PORTS": f"{20000 + os.getpid() % 400}-{20500}", "CAIRN_NIFI_SYNC_SECONDS": "30",
           "CAIRN_SECRETS_KEY": __import__("base64").b64encode(os.urandom(32)).decode()}
    os.environ.update(env)
    yield env
    for k in env:
        os.environ.pop(k, None)


@pytest.fixture(scope="module")
def lake(nifi_env, cairn, people):  # noqa: F811
    """Cairn Store with STS and Cairn Catalog deciding tables through Cairn, as test_spark_e2e's, the storage address
    handed to clients being the one containers reach (NiFi writes from its container)."""
    url, _ = cairn
    d = tempfile.mkdtemp()
    sp, cp = port(), port()
    store = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
        "CAIRN_STORE_DATA": f"{d}/s", "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse",
        "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_STS_SECRET": STS,
        "CAIRN_STORE_STS_PROVIDERS": json.dumps([{"issuer": s_issuer(cairn), "audiences": ["lakehouse"],
                                                  "jwks_url": f"{url}/v1/trino/jwks.json"}])})
    wait(sp)
    log = open(f"{d}/catalog.log", "w")
    catalog = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=log, stderr=subprocess.STDOUT, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c",
        "CAIRN_CATALOG_TOKEN": CATALOG_TOKEN, "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}",
        "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0], "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1],
        "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse", "CAIRN_CATALOG_FSYNC": "false",
        "CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables", "CAIRN_CATALOG_JWKS_URL": f"{url}/v1/trino/jwks.json",
        "CAIRN_CATALOG_CAIRN_URL": url, "CAIRN_CATALOG_CAIRN_TOKEN": SERVICE,
        "CAIRN_CATALOG_CLIENT_S3_ENDPOINT": f"http://host.docker.internal:{sp}"})
    wait(cp)
    import pyarrow as pa
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import IntegerType, NestedField, StringType

    admin = RestCatalog("admin", uri=f"http://127.0.0.1:{cp}", token=CATALOG_TOKEN)
    admin.create_namespace("crm")
    admin.create_table("crm.contacts", schema=Schema(
        NestedField(1, "id", IntegerType(), required=False), NestedField(2, "name", StringType(), required=False),
        NestedField(3, "email", StringType(), required=False)))
    assert pa  # pyarrow reads the landed rows below
    yield {"store": f"http://127.0.0.1:{sp}", "catalog": f"http://127.0.0.1:{cp}", "store_port": sp,
           "catalog_port": cp, "admin": admin, "dir": d, "log": f"{d}/catalog.log"}
    for p in (catalog, store):
        p.terminate()
        p.wait(15)
    shutil.rmtree(d, ignore_errors=True)


def _dump_nifi_logs() -> None:
    """NiFi's own log (the container's output and nifi-app.log's tail), straight to the terminal: called when a test
    here failed, before teardown removes the container it is in."""
    out = sys.__stderr__
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", "label=cairn.io/nifi-deployment"], capture_output=True,
                         text=True, timeout=60).stdout.split()
    for cid in ids:
        for title, cmd in (("docker logs", ["docker", "logs", "--tail", "300", cid]),
                           ("nifi-app.log", ["docker", "exec", cid, "tail", "-n", "400",
                                             "/opt/nifi/nifi-current/logs/nifi-app.log"])):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out.write(f"\n===== NiFi container {cid}: {title} =====\n{r.stdout}{r.stderr}\n")
    out.flush()


@pytest.fixture(scope="module")
def nf(nifi_env, lake, cairn, trino, request):  # noqa: F811
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

    url, s = cairn
    svc = s.nifi
    assert svc is not None, "managed NiFi was not built"
    p = url.rsplit(":", 1)[1]
    svc.cairn_url = f"http://host.docker.internal:{p}"
    svc.public_url = url
    svc.catalog_url = f"http://host.docker.internal:{lake['catalog_port']}"
    svc.store_url = f"http://host.docker.internal:{lake['store_port']}"
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    bundle = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies,
        Policy(id="ana-builds-connectors", effect="allow", actions=["DEPLOY_NIFI_FLOW", "VIEW_NIFI_DEPLOYMENT"],
               target=Target(resource_urns=["urn:li:nifiDeployment:*"]), condition=sub(ANA)),
        Policy(id="ana-lands-crm", effect="allow", condition=sub(ANA),
               actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET"], target=Target(resource_urns=["*crm.*"])),
        Policy(id="bob-reads-crm", effect="allow", condition=sub(BOB), actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*crm.*"])),
        Policy(id="mask-confidential", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["Confidential"], "strategy": "partial"})],
               condition={"all": [{"attr": "subject.urn", "op": "neq", "value": ANA},
                                  {"attr": "subject.urn", "op": "neq", "value": ADMIN}]})]))
    s.graph.aspects[(TABLE, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "name"},
        {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": "urn:li:glossaryTerm:Classification.Confidential.Email"}]}}]}
    ss = s.secret_scopes
    ss.create(ADMIN, {"name": "crm"})
    ss.put(ADMIN, "crm", "api_token", SECRET)
    ss.set_acl(ADMIN, "crm", ANA, "READ")
    failed = request.session.testsfailed
    yield {"url": url, "s": s, "svc": svc}
    if request.session.testsfailed > failed:
        _dump_nifi_logs()
    for name in (FLOW,):
        try:
            svc.delete_flow(ADMIN, name)
        except Exception:  # noqa: BLE001 - already gone
            pass
    try:
        svc.delete(ADMIN, DEP)
    except Exception:  # noqa: BLE001 - already gone
        pass
    subprocess.run("docker ps -aq --filter label=cairn.io/nifi-deployment | xargs -r docker rm -f", shell=True,
                   capture_output=True, timeout=120)


def api(nf, method, path, who=ADMIN, **kw):
    return httpx.request(method, f"{nf['url']}{path}", headers={"X-Cairn-Actor": who, **kw.pop("headers", {})},
                         timeout=900, **kw)


def flow_body(**kw) -> dict:
    from cairn.nifi.flowdef import lakehouse_template

    return {"name": FLOW, "deployment": DEP, "definition": lakehouse_template(VERSION),
            "description": "CRM contacts into the lakehouse", "domain": "urn:li:domain:crm",
            "parameters": {"sample.records": json.dumps(RECORDS)},
            "secrets": [{"parameter": "crm.api_token", "scope": "crm", "key": "api_token"}],
            "sink": {"type": "lakehouse", "namespace": "crm", "table": "contacts"},
            "lineage": {"inputs": [SOURCE], "outputs": []}, **kw}


def test_a_deployment_is_reached_only_through_cairn(nf):
    r = api(nf, "POST", "/v1/nifi/deployments", BOB, json={"name": DEP})
    assert r.status_code == 403 and "CREATE_NIFI_DEPLOYMENT" in r.text
    r = api(nf, "POST", "/v1/nifi/deployments", json={"name": DEP, "environment": "dev", "team": "crm",
                                                       "nifi_version": VERSION,
                                                       "resources": {"cpu": 2, "memory_mb": 3072, "heap_mb": 2048}})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["state"] == "ready" and d["canvas_url"].endswith(f"/v1/nifi/deployments/{DEP}/ui/nifi/"), d
    svc = nf["svc"]
    row = svc._row(DEP)
    inspect = json.loads(subprocess.run(["docker", "inspect", f"cairn-nifi-{DEP}"], capture_output=True, text=True,
                                        timeout=60, check=True).stdout)[0]
    keys = svc.keys.secrets(DEP, row.uid)
    assert not any(v in json.dumps(inspect["Config"]) for v in keys.values())     # no secret in its settings
    assert inspect["Config"]["User"] == "1000:1000"
    assert inspect["HostConfig"]["PortBindings"]["8443/tcp"][0]["HostIp"] == "127.0.0.1"
    # NiFi itself: TLS with the deployment's CA, and only Cairn's user, with Cairn's password.
    base = f"https://127.0.0.1:{row.port}/nifi-api"
    ca = svc.client(row).verify
    assert httpx.get(f"{base}/flow/about", verify=ca).status_code == 401
    bad = httpx.post(f"{base}/access/token", data={"username": "cairn", "password": "guess"}, verify=ca)
    assert bad.status_code in (400, 401, 403, 409)
    with pytest.raises(httpx.ConnectError):                        # nobody else's CA: not trusted
        httpx.get(f"{base}/flow/about", verify=True)
    # The canvas, through Cairn.
    r = api(nf, "GET", f"/v1/nifi/deployments/{DEP}/ui/nifi-api/flow/about")
    assert r.status_code == 200 and r.json()["about"]["version"] == VERSION, r.text
    page = api(nf, "GET", f"/v1/nifi/deployments/{DEP}/ui/nifi/")
    assert page.status_code == 200 and "<html" in page.text.lower()
    assert api(nf, "GET", f"/v1/nifi/deployments/{DEP}/ui/nifi-api/flow/about", BOB).status_code == 404
    r = api(nf, "POST", f"/v1/nifi/deployments/{DEP}/ui/nifi-api/access/token", ANA,
            data={"username": "cairn", "password": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    r = api(nf, "POST", f"/v1/nifi/deployments/{DEP}/ui/nifi-api/process-groups/root/process-groups", ANA,
            json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403                                   # a change from another site's page
    st = api(nf, "GET", f"/v1/nifi/deployments/{DEP}/status").json()
    assert st["health"] in ("healthy", "degraded") and st["nifi"]["version"] == VERSION, st


def test_a_flow_lands_in_the_lakehouse_as_its_owner_governed(nf, lake, trino):  # noqa: F811
    svc, s = nf["svc"], nf["s"]
    r = api(nf, "POST", "/v1/nifi/flows", BOB, json=flow_body())
    assert r.status_code == 403 and "DEPLOY_NIFI_FLOW" in r.text
    # A sink the owner may not write: refused before NiFi is touched.
    r = api(nf, "POST", "/v1/nifi/flows", ANA, json=flow_body(name="to_hr", sink={
        "type": "lakehouse", "namespace": "hr", "table": "salaries"}))
    assert r.status_code == 201 and r.json()["state"] == "failed" and "may not write hr.salaries" in r.json()["error"], (
        f"error: {r.json().get('error')} | {r.text}")
    assert api(nf, "DELETE", "/v1/nifi/flows/to_hr", ANA).status_code == 204
    r = api(nf, "POST", "/v1/nifi/flows", ANA, json=flow_body())
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["state"] == "running" and f["owner"] == ANA, f"error: {f.get('error')} | {f}"
    # The rows land (read as the admin through Trino, which reads the store with its own settings).
    end, rows = time.time() + 240, []
    while time.time() < end:
        rows = trino("SELECT id, email FROM crm.contacts ORDER BY id")
        if len(rows) >= 2:
            break
        time.sleep(3)
    assert [(x["id"], x["email"]) for x in rows] == [(1, "jane@acme.com"), (2, "bo@example.org")], (
        rows, svc.flow_status(ANA, FLOW), open(lake["log"]).read()[-3000:])
    # Its activity, read now (NiFi's counters cover five minutes): a run.
    RUN["run"] = api(nf, "POST", f"/v1/nifi/flows/{FLOW}/sync", ANA).json()["run"]
    # Written as ana, through her lakehouse session (the catalog asked Cairn about her, table by table).
    from cairn.dataaccess.lakehouse import lakehouse_sessions

    sid = svc._flow_row(FLOW).lake_session
    with s.engine.connect() as c:
        sess = c.execute(select(lakehouse_sessions).where(lakehouse_sessions.c.sid == sid)).first()
    assert sess.user == "ana" and sess.ended_s is None
    # Governed: a Confidential column is masked for bob through Trino, as it is for the owner and the admin.
    got = trino("SELECT id, email FROM crm.contacts ORDER BY id", user="bob")
    assert [x["email"] for x in got] == ["j•••@•••.com", "b•••@•••.org"], got
    assert [x["email"] for x in trino("SELECT email FROM crm.contacts ORDER BY id", user="ana")] == [
        "jane@acme.com", "bo@example.org"]


def test_the_flow_and_its_runs_are_in_cairn_core_and_observability(nf):
    from cairn.nifi import core
    from cairn.observability.pipelines import pipeline_runs

    s = nf["s"]
    g = s.graph
    furn = core.flow_urn(FLOW)
    assert g.get_aspect(furn, "dataFlowInfo")["customProperties"]["cairn.nifi.deployment"] == DEP
    assert {o["owner"] for o in g.get_aspect(furn, "ownership")["owners"]} == {ANA, "urn:li:corpGroup:crm"}
    assert g.get_aspect(furn, "domains") == {"domains": ["urn:li:domain:crm"]}
    root = core.job_urn(FLOW, ())
    io = g.get_aspect(root, "dataJobInputOutput")
    assert TABLE in io["outputDatasets"] and SOURCE in io["inputDatasets"]          # source -> table, one hop
    run = RUN.get("run")
    assert run and run["state"] == "success" and run["counts"]["processorFlowFilesOut"] >= 1, run
    runs = [u for (u, a), v in g.aspects.items() if a == "dataProcessInstanceRelationships" and v["parentTemplate"] == root]
    assert runs, "the run is not in Core"
    with s.engine.connect() as c:
        got = c.execute(select(pipeline_runs).where(pipeline_runs.c.pipeline == f"nifi:{FLOW}")).all()
    assert got and got[-1].state == "success" and TABLE in got[-1].outputs, got
    # Its status, with its processors (as NiFi reports them) and bulletins.
    st = api(nf, "GET", f"/v1/nifi/flows/{FLOW}/status", BOB)
    assert st.status_code == 404                                          # bob sees neither deployment nor flow
    st = api(nf, "GET", f"/v1/nifi/flows/{FLOW}/status", ANA).json()
    assert {p["name"] for p in st["processors"]} >= {"Sample records", "Land in the lakehouse"}, st
    # NiFi filters its bulletin board by this flow's groups (a pattern it searches for), with no limit: a pattern of
    # groups that do not exist matches nothing, and NiFi takes the pattern Cairn sends.
    from cairn.nifi.service import _groups_pattern

    nifi = nf["svc"].client(nf["svc"]._row(DEP))
    assert nifi.bulletins(None, None, _groups_pattern({"00000000-0000-0000-0000-000000000000"})) == []
    assert isinstance(nifi.bulletins(None, None, _groups_pattern({nf["svc"]._flow_row(FLOW).group_id})), list)


def test_a_shared_deployment_refuses_what_reaches_nifis_host_and_other_flows_contexts(nf):
    """On this shared deployment a flow that runs a command is refused; through the canvas, the owner cannot bind
    their own group to another parameter context (it would hand their processors another flow's secrets)."""
    svc = nf["svc"]
    d = flow_body()
    d["definition"]["flowContents"]["processors"].append({
        "identifier": "cmd", "name": "cmd", "type": "org.apache.nifi.processors.standard.ExecuteStreamCommand",
        "properties": {"Command Path": "cat", "Command Arguments": "conf/nifi.properties"}})
    r = api(nf, "POST", "/v1/nifi/flows", ANA, json={**d, "name": "hostly"})
    assert r.status_code == 403 and "is shared" in r.text, r.text
    gid = svc._flow_row(FLOW).group_id
    r = api(nf, "PUT", f"/v1/nifi/deployments/{DEP}/ui/nifi-api/process-groups/{gid}", ANA,
            json={"revision": {"version": 0}, "component": {"id": gid, "parameterContext": {"id": "not-hers"}}},
            headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403 and "its own parameter context" in r.text, r.text


def test_secrets_never_leave_cairn_in_the_clear(nf):
    svc = nf["svc"]
    row = svc._flow_row(FLOW)
    drow = svc._row(DEP)
    export = json.dumps(svc.client(drow).download(row.group_id))
    lake_secret_hint = "cairn.lakehouse.token"
    assert SECRET not in export and lake_secret_hint in export        # declared, never valued
    log = api(nf, "GET", f"/v1/nifi/deployments/{DEP}/logs?tail=5000").json()["log"]
    keys = svc.keys.secrets(DEP, drow.uid)
    assert SECRET not in log and not any(v in log for v in keys.values())
    assert "Generated Password" not in log                                # NiFi generated no credentials of its own
    kept = json.dumps(api(nf, "GET", f"/v1/nifi/flows/{FLOW}", ANA).json())
    assert SECRET not in kept


def test_the_token_nifi_holds_is_replaced_half_way_through_its_life(nf):
    """NiFi holds a token of the owner's session, never its credential: once half its life is gone Cairn replaces it
    (a renewal of the same session, audited) and NiFi restarts the catalog client and the sink with it."""
    svc, s = nf["svc"], nf["s"]
    row = svc._flow_row(FLOW)
    assert row.lake_token_s and not svc._rotate(row)                  # fresh: nothing to do
    svc._set_flow(FLOW, lake_token_s=row.lake_token_s - svc.lakehouse_sessions.ttl)
    before = {e["id"] for e in s.audit.entries("lakehouse", limit=1000)}
    assert svc._rotate(svc._flow_row(FLOW))
    after = svc._flow_row(FLOW)
    assert after.lake_session == row.lake_session and after.lake_token_s >= row.lake_token_s
    new = [e for e in s.audit.entries("lakehouse", limit=1000) if e["id"] not in before]
    assert any(e["action"] == "lakehouse.token_refreshed" and (e.get("details") or {}).get("with") == "nifi"
               for e in new), new
    st = svc.flow_status(ANA, FLOW)
    assert {p["name"]: p["run_status"] for p in st["processors"]}.get("Land in the lakehouse") == "Running", st
    export = json.dumps(svc.client(svc._row(DEP)).download(after.group_id))
    assert "cairn.lakehouse.token" in export and SECRET not in export


def test_deleting_cleans_up(nf):
    from cairn.dataaccess.lakehouse import lakehouse_sessions
    from cairn.nifi import core

    svc, s = nf["svc"], nf["s"]
    row = svc._flow_row(FLOW)
    assert api(nf, "DELETE", f"/v1/nifi/flows/{FLOW}", BOB).status_code in (403, 404)
    assert api(nf, "DELETE", f"/v1/nifi/flows/{FLOW}", ANA).status_code == 204
    assert svc.client(svc._row(DEP)).find_group(f"cairn-{FLOW}") is None
    with s.engine.connect() as c:
        sess = c.execute(select(lakehouse_sessions).where(lakehouse_sessions.c.sid == row.lake_session)).first()
    assert sess.ended_s is not None
    assert s.graph.get_aspect(core.flow_urn(FLOW), "status") == {"removed": True}
    assert api(nf, "DELETE", f"/v1/nifi/deployments/{DEP}", ANA).status_code == 403
    assert api(nf, "DELETE", f"/v1/nifi/deployments/{DEP}").status_code == 204
    left = subprocess.run(["docker", "ps", "-a", "--filter", f"label=cairn.io/nifi-deployment={DEP}", "-q"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=cairn.io/nifi-deployment={DEP}"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    assert not left and not vols
