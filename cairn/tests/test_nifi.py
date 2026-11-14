"""Managed NiFi (L152) without NiFi: what a deployment and a flow may be, the secrets rules of flow definitions, the
parameters Cairn adds, lineage read from processors' settings, the keys and files each deployment gets, the Docker
and Kubernetes renders, the privileges (deny by default, fail closed) of the API and the canvas through Cairn, and
what reaches Cairn Core. The real NiFi is tests/test_nifi_e2e.py."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert
from starlette.requests import Request  # module level: the test router's dependency is annotated with it

from cairn.airflow_deploy.spec import SpecError
from cairn.common.core import InMemoryGraph
from cairn.common.db import metadata
from cairn.nifi import core, flowdef
from cairn.nifi.keys import START, Keys
from cairn.nifi.service import NiFiService, NiFiServiceError, deployments_table, flows_table
from cairn.nifi.spec import check_deployment, check_flow
from cairn.sources.openlineage import OpenLineageReceiver, job_urns

KEY = Keys(b"nifi-unit-test-key-" + b"k" * 16)
V = ("2.12.0",)
PG_URL = "jdbc:postgresql://crm-db:5432/crm"


def template() -> dict:
    return flowdef.lakehouse_template("2.12.0")


def lake_flow(**kw) -> dict:
    return {"deployment": "ingest-dev", "definition": template(), "parameters": {"sample.records": "[{\"id\": 1}]"},
            "sink": {"type": "lakehouse", "namespace": "crm", "table": "contacts"}, **kw}


# ---- what may be asked --------------------------------------------------------------------------------------------
def test_a_deployment_spec_has_sizes_that_fit_and_tested_versions_only():
    d = check_deployment({"name": "ingest-dev"}, None, V)
    assert d["environment"] == "dev" and d["nifi_version"] == "2.12.0"
    assert d["resources"] == {"cpu": 1.0, "memory_mb": 4096, "heap_mb": 2048, "storage_gb": 20}
    with pytest.raises(SpecError, match="nifi_version"):
        check_deployment({"nifi_version": "1.28.0"}, None, V)
    with pytest.raises(SpecError, match="three quarters"):
        check_deployment({"resources": {"memory_mb": 4096, "heap_mb": 4000}}, None, V)
    with pytest.raises(SpecError, match="only grows"):
        check_deployment({"resources": {"storage_gb": 10}}, d, V)
    bigger = check_deployment({"resources": {"cpu": 2, "memory_mb": 8192, "heap_mb": 4096}}, d, V)
    assert bigger["resources"]["storage_gb"] == 20 and bigger["resources"]["memory_mb"] == 8192
    with pytest.raises(SpecError, match="unknown fields"):
        check_deployment({"admin_password": "x"}, None, V)


def test_flow_secrets_are_references_never_values():
    f = check_flow(lake_flow(secrets=[{"parameter": "crm.password", "scope": "crm", "key": "password"}]))
    assert f["secrets"] == [{"parameter": "crm.password", "scope": "crm", "key": "password"}]
    for bad, why in (({"secrets": [{"parameter": "p", "value": "hunter2"}]}, "never holds"),
                     ({"parameters": {"url": "postgres://app:s3cret@db/crm"}}, "looks like a credential"),
                     ({"parameters": {"cairn.lakehouse.token": "x"}}, "set by Cairn"),
                     ({"secrets": [{"parameter": "cairn.lakehouse.token", "scope": "a", "key": "b"}]}, "Cairn"),
                     ({"sink": {"type": "s3", "bucket": "x"}}, "sink is"),
                     ({"sink": {"type": "lakehouse", "namespace": "CRM", "table": "x"}}, "namespace"),
                     ({"lineage": {"inputs": ["not-an-urn"]}}, "dataset URNs")):
        with pytest.raises(SpecError, match=why):
            check_flow(lake_flow(**bad))


def test_definitions_with_secrets_in_them_are_refused():
    d = template()
    assert flowdef.check_definition(d) is d
    cat = next(s for s in d["flowContents"]["controllerServices"] if s["type"].endswith("RESTIcebergCatalog"))
    cat["properties"]["Bearer Token"] = "the-real-secret"           # a sensitive property with a value
    with pytest.raises(SpecError, match="sensitive: reference a sensitive parameter"):
        flowdef.check_definition(d)
    d = template()
    d["parameterContexts"] = {"ctx": {"parameters": [{"name": "pw", "sensitive": True, "value": "hunter2"}]}}
    with pytest.raises(SpecError, match="sensitive and has a value"):
        flowdef.check_definition(d)
    d = template()
    gen = d["flowContents"]["processors"][0]
    gen["properties"]["Custom Text"] = "token ghp_" + "a" * 30
    with pytest.raises(SpecError, match="looks like a credential"):
        flowdef.check_definition(d)
    with pytest.raises(SpecError, match="flowContents"):
        flowdef.check_definition({"processors": []})


def test_prepare_declares_every_parameter_in_cairn_s_context():
    cparams = {"cairn.lakehouse.catalog_uri": "http://catalog:8181", "cairn.lakehouse.token_uri": "http://c/t",
               "cairn.lakehouse.store_endpoint": "http://store:9000", "cairn.lakehouse.namespace": "crm",
               "cairn.lakehouse.table": "contacts", "cairn.lakehouse.token": ""}
    out = flowdef.prepare(template(), "contacts", {"sample.records": "[]"}, [], cparams)
    assert out["flowContents"]["parameterContextName"] == "cairn-contacts"
    params = {p["name"]: p for p in out["parameterContexts"]["cairn-contacts"]["parameters"]}
    assert params["cairn.lakehouse.token"]["sensitive"] and "value" not in params["cairn.lakehouse.token"]
    assert params["cairn.lakehouse.catalog_uri"] == {"name": "cairn.lakehouse.catalog_uri", "description": "",
                                                     "sensitive": False, "provided": False,
                                                     "value": "http://catalog:8181"}
    with pytest.raises(SpecError, match="references parameters nobody sets: sample.records"):
        flowdef.prepare(template(), "contacts", {}, [], cparams)
    # A sensitive property that references a plain parameter is refused (NiFi would refuse it at the upload).
    d = template()
    gen = d["flowContents"]["processors"][0]
    gen["propertyDescriptors"]["Custom Text"]["sensitive"] = True
    with pytest.raises(SpecError, match="make them secrets"):
        flowdef.prepare(d, "contacts", {"sample.records": "[]"}, [], cparams)
    assert flowdef.prepare(d, "contacts", {}, ["sample.records"], cparams)


def _proc(t: str, props: dict, group: str = "") -> dict:
    return {"identifier": t + group, "name": t, "type": f"org.apache.nifi.processors.x.{t}", "properties": props}


def test_lineage_comes_from_processors_settings():
    pool = {"identifier": "pool", "name": "crm", "type": "org.apache.nifi.dbcp.DBCPConnectionPool",
            "properties": {"Database Connection URL": "#{crm.url}"}}
    kafka = {"identifier": "kafka", "name": "kafka", "type": "org.apache.nifi.kafka.service.Kafka3ConnectionService",
             "properties": {"bootstrap.servers": "broker:9092"}}
    nested = {"name": "publish", "processors": [
        _proc("PublishKafka", {"Kafka Connection Service": "kafka", "Topic Name": "contacts.changed"}, "n"),
        _proc("PutS3Object", {"Bucket": "raw", "Object Key": "crm/contacts/${filename}"}, "n")],
        "controllerServices": [], "processGroups": []}
    d = {"flowContents": {"name": "root", "controllerServices": [pool, kafka], "processGroups": [nested], "processors": [
        _proc("ExecuteSQLRecord", {"Database Connection Pooling Service": "pool",
                                   "SQL Query": "SELECT c.id, c.email FROM sales.customers c JOIN accounts a ON 1=1"}),
        _proc("QueryDatabaseTableRecord", {"Database Connection Pooling Service": "pool", "Table Name": "orders"}),
        _proc("ConsumeKafka", {"Kafka Connection Service": "kafka", "Topics": "a, b"}),
        _proc("GetFile", {"Input Directory": "/data/in/${now()}"}),           # computed per FlowFile: no dataset
        _proc("PutIcebergRecord", {"Namespace": "#{ns}", "Table Name": "contacts"})]}}
    got = flowdef.lineage(d, {"crm.url": PG_URL, "ns": "crm"})
    root = got[()]
    assert {"namespace": "postgres://crm-db:5432", "name": "crm.sales.customers"} in root["inputs"]
    assert {"namespace": "postgres://crm-db:5432", "name": "crm.public.accounts"} in root["inputs"]
    assert {"namespace": "postgres://crm-db:5432", "name": "crm.public.orders"} in root["inputs"]
    assert {"namespace": "kafka://broker:9092", "name": "b"} in root["inputs"]
    assert not [x for x in root["inputs"] if x["namespace"].startswith("file")]
    assert root["outputs"] == [{"namespace": "lakehouse", "name": "crm.contacts"}]
    assert got[("publish",)]["outputs"] == [{"namespace": "kafka://broker:9092", "name": "contacts.changed"},
                                            {"namespace": "s3://raw", "name": "crm/contacts"}]


# ---- keys and files -----------------------------------------------------------------------------------------------
def test_a_deployment_s_keys_are_derived_and_its_files_hold_no_secret_in_the_clear():
    import bcrypt
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12

    s1, s2 = KEY.secrets("ingest", "u1"), KEY.secrets("ingest", "u2")
    assert s1 == KEY.secrets("ingest", "u1") and s1["password"] != s2["password"] and len(s1["password"]) >= 40
    files = KEY.files("ingest", "u1", ["cairn-nifi-ingest", "127.0.0.1"])
    again = KEY.files("ingest", "u1", ["cairn-nifi-ingest", "127.0.0.1"])     # the same each time (certificates
    fixed = lambda fs: {k: v for k, v in fs.items() if not k.endswith((".p12", ".crt"))}  # noqa: E731
    assert fixed(files) == fixed(again)                                        # and stores: signed and salted
    pub = lambda fs: x509.load_pem_x509_certificate(fs["opt/cairn/ca.crt"][0]).public_key().public_numbers()  # noqa: E731
    assert pub(files) == pub(again)                                            # afresh, with the same keys)
    modes = {p.rsplit("/", 1)[-1]: m for p, (_, m) in files.items()}
    assert modes == {"nifi.secrets": 0o400, "keystore.p12": 0o400, "truststore.p12": 0o400,
                     "login-identity-providers.xml": 0o400, "start.sh": 0o500, "ca.crt": 0o444}
    xml = files["opt/cairn/login-identity-providers.xml"][0].decode()
    assert s1["password"] not in xml and "<property name=\"Username\">cairn</property>" in xml
    hashed = xml.split('<property name="Password">')[1].split("<")[0]
    assert hashed.startswith("$2b$12$") and bcrypt.checkpw(s1["password"].encode(), hashed.encode())
    key, cert, cas = pkcs12.load_key_and_certificates(files["opt/cairn/keystore.p12"][0], s1["tls_password"].encode())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "cairn-nifi-ingest" in sans.get_values_for_type(x509.DNSName)
    ca = x509.load_pem_x509_certificate(files["opt/cairn/ca.crt"][0])
    assert cert.issuer == ca.subject and cas and cas[0] == ca
    assert pkcs12.load_pkcs12(files["opt/cairn/truststore.p12"][0], s1["tls_password"].encode())
    with pytest.raises(ValueError):
        pkcs12.load_key_and_certificates(files["opt/cairn/keystore.p12"][0], b"wrong")
    # The start script reads the secrets with shell builtins: it holds none, and passes none on a command line.
    assert s1["tls_password"] not in START and s1["sensitive_key"] not in START
    for line in START.splitlines():
        if "${tls}" in line or "${skey}" in line:
            assert line.strip().startswith(('"nifi.', "printf")), line


def test_the_docker_container_runs_as_nifi_s_user_on_loopback_with_no_secret_in_its_environment():
    from cairn.nifi.docker_runtime import DockerRuntime, env

    rt = DockerRuntime(socket="/nonexistent.sock")
    spec = check_deployment({"resources": {"cpu": 2, "memory_mb": 6144, "heap_mb": 3072}}, None, V)
    dep = {"name": "ingest", "uid": "u1", "port": 18443, "spec": spec, "proxy_hosts": rt.proxy_hosts(
        {"name": "ingest", "port": 18443}), "context_path": "/cairn/api/v1/nifi/deployments/ingest/ui"}
    dep["files"] = KEY.files("ingest", "u1", rt.tls_hosts(dep))
    body = rt._body(dep, "apache/nifi:2.12.0")
    assert body["User"] == "1000:1000" and body["Entrypoint"] == ["bash", "/opt/cairn/start.sh"]
    assert body["HostConfig"]["PortBindings"]["8443/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": "18443"}]
    assert body["HostConfig"]["CapDrop"] == ["ALL"] and body["HostConfig"]["Memory"] == 6144 * 1024 * 1024
    assert "NIFI_JVM_HEAP_MAX=3072m" in body["Env"]
    secrets = KEY.secrets("ingest", "u1")
    assert not any(v in json.dumps(body) for v in secrets.values())
    assert not any(k.startswith(("SINGLE_USER", "NIFI_SENSITIVE", "KEYSTORE")) for k in (e.split("=")[0] for e in env(
        dep, [], "")))
    assert {m["Target"] for m in body["HostConfig"]["Mounts"]} >= {"/opt/nifi/nifi-current/conf",
                                                                   "/opt/nifi/nifi-current/flowfile_repository"}


def test_the_kubernetes_render_is_non_root_reached_only_from_cairn_and_its_secrets_are_files():
    from cairn.nifi.k8s_runtime import KubernetesRuntime

    rt = KubernetesRuntime(api="https://127.0.0.1:6443", token_file="/nonexistent", cairn_namespace="cairn")
    spec = check_deployment({}, None, V)
    dep = {"name": "ingest", "uid": "u1", "port": None, "spec": spec, "context_path": "/cairn/api/v1/nifi/x/ui"}
    dep["proxy_hosts"] = rt.proxy_hosts(dep)
    dep["files"] = KEY.files("ingest", "u1", rt.tls_hosts(dep))
    objs = {(o["kind"], o["metadata"]["name"]): o for o in rt.render(dep, "apache/nifi:2.12.0")}
    ns = objs[("Namespace", "cairn-nifi-ingest")]
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    sts = objs[("StatefulSet", "nifi")]
    pod = sts["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True
    c = pod["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"] == {
        "drop": ["ALL"]}
    assert not any(v in json.dumps(c["env"]) for v in KEY.secrets("ingest", "u1").values())
    assert {"name": "cairn", "secret": {"secretName": "cairn-nifi-files", "defaultMode": 0o440}} in pod["volumes"]
    assert sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "20Gi"
    pol = objs[("NetworkPolicy", "cairn-nifi-ingress")]["spec"]["ingress"][0]["from"]
    assert pol == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cairn"}}}]
    assert set(objs[("Secret", "cairn-nifi-files")]["data"]) == {"nifi.secrets", "keystore.p12", "truststore.p12",
                                                                  "login-identity-providers.xml", "start.sh", "ca.crt"}
    assert rt.api_url(dep) == "https://nifi.cairn-nifi-ingest.svc:8443"
    # A new render that drops something prunes it, but never the StatefulSet's volume or a Secret.
    calls = []
    rt._call = lambda m, path, *a, **kw: (calls.append((m, path)) or {"items": [{"metadata": {"name": "old"}}]}) \
        if m == "GET" else calls.append((m, path))
    rt._prune(dep, list(objs.values()))
    deleted = [p for m, p in calls if m == "DELETE"]
    assert deleted and not [p for p in deleted if "persistentvolumeclaims" in p or "/secrets/" in p]


# ---- privileges ---------------------------------------------------------------------------------------------------
class Policy:
    def __init__(self, grants: dict[str, set[str]], broken: bool = False):
        self.grants, self.broken = grants, broken

    def __call__(self, actor, privilege, target):
        if self.broken:
            raise RuntimeError("the policy decision point is down")
        return privilege in self.grants.get(actor, set())


ANA, BOB, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:eve"


@pytest.fixture
def svc(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n.db")
    metadata.create_all(engine, tables=[deployments_table, flows_table])
    now = int(time.time() * 1000)
    with engine.begin() as c:
        c.execute(insert(deployments_table).values(name="ingest-dev", uid="u1", runtime="docker", state="ready",
                                                   spec=json.dumps(check_deployment({"team": "crm"}, None, V)),
                                                   port=18443, ca_pem="x", created_by=ANA, created_ms=now,
                                                   updated_ms=now))
    policy = Policy({ANA: {"DEPLOY_NIFI_FLOW", "VIEW_NIFI_DEPLOYMENT"}, BOB: {"VIEW_NIFI_DEPLOYMENT"}})
    s = NiFiService(engine, policy, keys=KEY, runtime=SimpleNamespace(name="docker", images={"2.12.0": "i"}),
                    public_url="https://cairn.example.com", graph=InMemoryGraph())
    s.policy = policy
    return s


def test_deny_by_default_and_fail_closed(svc):
    assert [d["name"] for d in svc.list(ANA)] == ["ingest-dev"]
    assert svc.list(EVE) == []
    with pytest.raises(NiFiServiceError, match="no such deployment") as e:
        svc.get(EVE, "ingest-dev")
    assert e.value.status == 404
    with pytest.raises(NiFiServiceError, match="CREATE_NIFI_DEPLOYMENT"):
        svc.create(ANA, {"name": "other"})
    with pytest.raises(NiFiServiceError, match="DEPLOY_NIFI_FLOW"):
        svc.create_flow(BOB, {"name": "contacts", **lake_flow()})
    with pytest.raises(NiFiServiceError, match="MANAGE_NIFI_DEPLOYMENT"):
        svc.update(ANA, "ingest-dev", {"resources": {"cpu": 2}})
    svc.policy.broken = True                         # the PDP cannot decide: refused, never allowed
    assert svc.list(ANA) == []
    with pytest.raises(NiFiServiceError):
        svc.create_flow(ANA, {"name": "contacts", **lake_flow()})


def test_a_flow_is_its_owner_s_and_its_secrets_are_read_as_them(svc):
    with pytest.raises(NiFiServiceError, match="looks like a credential"):
        svc.create_flow(ANA, {"name": "bad", **lake_flow(parameters={"u": "https://a:b@host/x"})})
    reads = []
    svc.secret_scopes = SimpleNamespace(enabled=True, read=lambda who, scope, key: reads.append(who) or (_ for _ in ())
                                        .throw(PermissionError("no READ on crm")))
    out = svc.create_flow(ANA, {"name": "contacts", **lake_flow(
        secrets=[{"parameter": "crm.password", "scope": "crm", "key": "password"}])})
    # The secret could not be read as the owner: not deployed, and why is recorded.
    assert reads == [ANA] and out["state"] == "failed" and "cannot be read by the flow's owner" in out["error"]
    assert out["owner"] == ANA and "definition" in out
    svc.policy.grants[BOB] = {"DEPLOY_NIFI_FLOW", "VIEW_NIFI_DEPLOYMENT"}
    with pytest.raises(NiFiServiceError, match="only the flow's owner") as e:
        svc.update_flow(BOB, "contacts", {"parameters": {"sample.records": "[]"}})
    assert e.value.status == 403
    with pytest.raises(NiFiServiceError, match="MANAGE_NIFI_DEPLOYMENT"):
        svc.delete_flow(BOB, "contacts")                # not the owner, and not the deployment's manager
    assert [f["name"] for f in svc.flows(BOB)] == ["contacts"]
    assert svc.flows(EVE) == []
    full = svc.flows(ANA, full=True)
    assert full[0]["definition"]["flowContents"]["processors"]


def test_a_lakehouse_sink_is_refused_when_the_owner_may_not_write_the_table(svc):
    svc.secret_scopes = None
    svc.catalog_url, svc.store_url = "http://catalog:8181", "http://store:9000"
    svc.lakehouse_sessions = SimpleNamespace(issue=lambda who: pytest.fail("no session for a refused sink"))
    svc.lakehouse = SimpleNamespace(decide=lambda who, tables: {"tables": [{"view": True, "read": True,
                                                                             "write": False, "restricted": False}]})
    svc._clients["ingest-dev:u1"] = SimpleNamespace(find_group=lambda n: None)
    out = svc.create_flow(ANA, {"name": "contacts", **lake_flow()})
    assert out["state"] == "failed" and "may not write crm.contacts" in out["error"]
    svc.lakehouse = SimpleNamespace(decide=lambda who, tables: {"tables": [{"view": True, "read": True,
                                                                             "write": True, "restricted": True}]})
    with pytest.raises(NiFiServiceError, match="masks or row filters apply"):
        svc.deploy(ANA, "contacts")


def test_the_canvas_through_cairn_decides_every_request(svc):
    p = svc.proxy_privilege
    assert p("GET", "nifi/") == "VIEW" and p("GET", "nifi-api/flow/process-groups/root") == "VIEW"
    assert p("POST", "nifi-api/process-groups/x/processors") == "DEPLOY_NIFI_FLOW"
    assert p("PUT", "nifi-api/controller/config") == "MANAGE_NIFI_DEPLOYMENT"
    assert p("POST", "nifi-api/parameter-contexts/x/update-requests") == "MANAGE_NIFI_DEPLOYMENT"
    assert p("POST", "nifi-api/access/token") is None and p("GET", "nifi-api/access/token/expiration") is None
    with pytest.raises(NiFiServiceError) as e:
        svc.proxy(EVE, "ingest-dev", "GET", "nifi/", "", {}, b"", True, True)
    assert e.value.status == 404                       # nothing to see: not even that it exists
    with pytest.raises(NiFiServiceError, match="DEPLOY_NIFI_FLOW"):
        svc.proxy(BOB, "ingest-dev", "POST", "nifi-api/process-groups/x/processors", "", {}, b"{}", True, True)
    with pytest.raises(NiFiServiceError, match="same origin"):
        svc.proxy(ANA, "ingest-dev", "POST", "nifi-api/process-groups/x/processors", "", {}, b"{}", True, False)
    with pytest.raises(NiFiServiceError, match="sign-in is not reachable"):
        svc.proxy(ANA, "ingest-dev", "POST", "nifi-api/access/token", "", {}, b"username=cairn", True, True)


def test_the_canvas_route_signs_in_and_refuses_without_the_privilege(svc):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from cairn.nifi.service import build_nifi_router

    def principal(request: Request):
        who = request.headers.get("x-test-actor")
        if not who:
            raise HTTPException(401, "sign in")
        return SimpleNamespace(urn=who)

    app = FastAPI()
    app.include_router(build_nifi_router(svc, principal))
    c = TestClient(app)
    assert c.get("/v1/nifi/deployments/ingest-dev/ui/nifi/").status_code == 401
    assert c.get("/v1/nifi/deployments/ingest-dev/ui/nifi/", headers={"x-test-actor": EVE}).status_code == 404
    r = c.post("/v1/nifi/deployments/ingest-dev/ui/nifi-api/process-groups/x/processors", json={},
               headers={"x-test-actor": BOB, "authorization": "Bearer t"})
    assert r.status_code == 403 and "DEPLOY_NIFI_FLOW" in r.json()["detail"]
    r = c.post("/v1/nifi/deployments/ingest-dev/ui/nifi-api/access/token", data={"username": "cairn"},
               headers={"x-test-actor": ANA, "sec-fetch-site": "same-origin"})
    assert r.status_code == 403
    assert c.get("/v1/nifi/flow-templates/lakehouse", headers={"x-test-actor": BOB}).json()["flowContents"]


# ---- Cairn Core ---------------------------------------------------------------------------------------------------
def test_job_names_split_into_flow_and_process_group_only_for_nifi():
    assert job_urns("nifi://ingest-dev", "contacts.group:a/b")[1:] == (
        "urn:li:dataFlow:(nifi,contacts,PROD)", "urn:li:dataJob:(urn:li:dataFlow:(nifi,contacts,PROD),group:a/b)")
    assert job_urns("unified-platform://x", "a.b")[0] != "nifi"


def test_flows_their_groups_lineage_and_runs_reach_cairn_core():
    g = InMemoryGraph()
    ds = core.Datasets(lambda t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)")
    inferred = {(): {"inputs": [{"namespace": "postgres://crm-db:5432", "name": "crm.public.contacts"}], "outputs": []},
                ("enrich",): {"inputs": [], "outputs": [{"namespace": "kafka://b:9092", "name": "contacts.changed"}]}}
    declared = {"inputs": ["urn:li:dataset:(urn:li:dataPlatform:salesforce,Contact,PROD)"], "outputs": []}
    sink = {"type": "lakehouse", "namespace": "crm", "table": "contacts"}
    io = core.groups_io(inferred, declared, sink, ds)
    table = "urn:li:dataset:(urn:li:dataPlatform:iceberg,crm.contacts,PROD)"
    src = "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.public.contacts,PROD)"
    assert table in io[()]["outputs"] and src in io[()]["inputs"] and declared["inputs"][0] in io[()]["inputs"]
    core.register(g, "contacts", "ingest-dev", ANA, {"sink": sink, "description": "CRM contacts"}, io, "crm",
                  "urn:li:domain:sales")
    furn = "urn:li:dataFlow:(nifi,contacts,PROD)"
    assert g.get_aspect(furn, "dataFlowInfo")["customProperties"]["cairn.nifi.sink"] == "crm.contacts"
    assert {o["owner"] for o in g.get_aspect(furn, "ownership")["owners"]} == {ANA, "urn:li:corpGroup:crm"}
    assert g.get_aspect(furn, "domains") == {"domains": ["urn:li:domain:sales"]}
    root = core.job_urn("contacts", ())
    assert root == "urn:li:dataJob:(urn:li:dataFlow:(nifi,contacts,PROD),flow)"
    assert table in g.get_aspect(root, "dataJobInputOutput")["outputDatasets"]
    assert src in g.get_aspect(root, "dataJobInputOutput")["inputDatasets"]
    assert g.get_aspect(core.job_urn("contacts", ("enrich",)), "dataJobInfo")["flowUrn"] == furn
    # A window of activity: run events through Cairn's OpenLineage receiver, each group a task of the flow's run.
    rec = OpenLineageReceiver(g)
    seen = []
    rec.observers.append(lambda ev, job, i, o: seen.append((job, ev["eventType"], ev["run"]["facets"]["parent"]["run"]["runId"])))
    run = "7f1d2c3b-0000-4000-8000-000000000001"
    ol_io = {(): {"inputs": [inferred[()]["inputs"][0]], "outputs": [ds.ol({"namespace": "lakehouse",
                                                                             "name": "crm.contacts"})]},
             ("enrich",): {"inputs": [], "outputs": inferred[("enrich",)]["outputs"]}}
    assert ol_io[()]["outputs"] == [{"namespace": "iceberg", "name": "crm.contacts"}]
    for ev in core.run_events("contacts", "ingest-dev", run, 1_000, 61_000, {()}, ol_io,
                              "PutIcebergRecord failed"):
        rec.handle(ev)
    assert {(j, t) for j, t, _ in seen} == {(root, "START"), (root, "FAIL"), (core.job_urn("contacts", ("enrich",)),
                                                                             "START"),
                                            (core.job_urn("contacts", ("enrich",)), "COMPLETE")}
    assert {r for _, _, r in seen} == {run}
    runs = [u for (u, a) in g.aspects if a == "dataProcessInstanceRelationships"]
    assert len(runs) == 2 and table in g.get_aspect(root, "dataJobInputOutput")["outputDatasets"]
    results = [e for (u, a), v in g.timeseries.items() if a == "dataProcessInstanceRunEvent" for e in v]
    assert {(e.get("result") or {}).get("type") for e in results} >= {"FAILURE", "SUCCESS"}
    core.unregister(g, "contacts", [(), ("enrich",)])
    assert g.get_aspect(furn, "status") == {"removed": True}


def test_the_proxy_refuses_paths_that_would_reach_another_endpoint_than_the_one_decided():
    """The privilege is decided on the path as sent, so a path NiFi would resolve elsewhere is never forwarded:
    "/nifi-api/flow/../controller" would pass as a flow change (DEPLOY_NIFI_FLOW) and reach NiFi's settings."""
    from cairn.nifi.service import proxy_path_ok

    for good in ("nifi-api/flow/process-groups/root", "/nifi-api/controller/config", "nifi/index.html",
                 "nifi-api/flow/search-results"):          # (the query string reaches the proxy apart from the path)
        assert proxy_path_ok(good), good
    for bad in ("nifi-api/flow/../controller/config", "nifi-api/flow/%2e%2e/controller/config",
                "nifi-api/flow/%252e%252e/controller/config", "nifi-api/./controller", "nifi-api//controller",
                "nifi-api\\controller", "nifi-api/flow/%2E%2E/access/token", "nifi-api/flow\x00/x",
                "nifi-api/%63ontroller/config", "nifi-api/%70arameter-contexts/x"):   # NiFi would decode these
        assert not proxy_path_ok(bad), bad


def test_privileges_are_decided_on_whole_path_segments(svc):
    """NiFi-wide resources need MANAGE_NIFI_DEPLOYMENT whatever their name starts with; a flow's own controller
    services are a flow part (not the controller); reads of what flowed through NiFi need MANAGE too."""
    p = svc.proxy_privilege
    for path in ("nifi-api/reporting-tasks/x", "nifi-api/reporting-tasks/x/run-status", "nifi-api/parameter-providers/x",
                 "nifi-api/flow-analysis-rules/x", "nifi-api/controller/config", "nifi-api/tenants/users",
                 "nifi-api/counters/x", "nifi-api/some-future-resource/x"):
        assert p("PUT", path) == "MANAGE_NIFI_DEPLOYMENT", path
    assert p("PUT", "nifi-api/controller-services/x") == "DEPLOY_NIFI_FLOW"
    assert p("PUT", "nifi-api/processors/x") == "DEPLOY_NIFI_FLOW"
    assert p("GET", "nifi-api/provenance-events/1/content/output") == "MANAGE_NIFI_DEPLOYMENT"
    assert p("GET", "nifi-api/flowfile-queues/c/flowfiles/f/content") == "MANAGE_NIFI_DEPLOYMENT"
    assert p("GET", "nifi-api/processors/x") == "VIEW"
    assert p("PUT", "nifi/index.html") is None                         # the canvas's files are read only


class _Canvas:
    """NiFi's process groups and components, as the proxy asks about them."""
    def __init__(self, parents: dict, components: dict):
        self.parents, self.components = parents, components

    def root(self):
        return "root"

    def group(self, gid):
        if gid not in self.parents:
            raise NiFiServiceError("no such group", 404)
        return {"component": {"id": gid, "parentGroupId": self.parents[gid], "parameterContext": {"id": f"ctx-{gid}"}}}

    def call(self, method, path, **kw):
        kind, cid = path.strip("/").split("/")[:2]
        return {"component": {"id": cid, "parentGroupId": self.components[(kind, cid)]}}


def test_a_change_through_the_canvas_is_the_flow_owners(svc):
    """Changing a part of a flow takes its owner (with DEPLOY_NIFI_FLOW) or MANAGE_NIFI_DEPLOYMENT: another deployer
    cannot rewire someone else's flow, which runs with its owner's secrets and lakehouse session."""
    now = int(time.time() * 1000)
    with svc.engine.begin() as c:
        for name, owner, gid in (("anas", ANA, "g-ana"), ("bobs", BOB, "g-bob")):
            c.execute(insert(flows_table).values(name=name, deployment="ingest-dev", owner=owner, spec="{}",
                                                 definition="{}", version=1, state="running", group_id=gid,
                                                 created_by=owner, created_ms=now, updated_ms=now))
    svc.policy.grants[BOB] = {"DEPLOY_NIFI_FLOW", "VIEW_NIFI_DEPLOYMENT"}
    canvas = _Canvas(parents={"g-ana": "root", "g-bob": "root", "g-ana-inner": "g-ana"},
                     components={("processors", "p-ana"): "g-ana-inner", ("processors", "p-bob"): "g-bob",
                                 ("connections", "c-ana"): "g-ana", ("controller-services", "cs-ana"): "g-ana"})
    row = svc._row("ingest-dev")
    ok = lambda who, method, path, body=b"": svc._may_change_part(who, row, canvas, method,  # noqa: E731
                                                                  path.split("/"), body)
    ok(ANA, "PUT", "nifi-api/processors/p-ana")                       # deep inside her own flow
    ok(ANA, "PUT", "nifi-api/controller-services/cs-ana")
    ok(ANA, "POST", "nifi-api/flowfile-queues/c-ana/drop-requests")
    ok(ANA, "POST", "nifi-api/process-groups/g-ana-inner/processors")
    ok(BOB, "PUT", "nifi-api/processors/p-bob")
    for path in ("nifi-api/processors/p-ana", "nifi-api/controller-services/cs-ana",
                 "nifi-api/process-groups/g-ana/processors", "nifi-api/flow/process-groups/g-ana",
                 "nifi-api/flowfile-queues/c-ana/listing-requests"):
        with pytest.raises(NiFiServiceError, match="anas is .*only its owner"):
            ok(BOB, "PUT", path)
    with pytest.raises(NiFiServiceError, match="not in a flow of yours"):
        ok(ANA, "POST", "nifi-api/process-groups/root/processors")    # the root group is no one's flow
    with pytest.raises(NiFiServiceError, match="deleting the flow in Cairn"):
        ok(ANA, "DELETE", "nifi-api/process-groups/g-ana")
    svc.policy.grants[BOB] = {"MANAGE_NIFI_DEPLOYMENT"}
    ok(BOB, "PUT", "nifi-api/processors/p-ana")                       # the deployment's manager may


def test_an_owner_whose_right_was_taken_away_cannot_restart_their_flow(svc):
    now = int(time.time() * 1000)
    with svc.engine.begin() as c:
        c.execute(insert(flows_table).values(name="anas", deployment="ingest-dev", owner=ANA, spec="{}",
                                             definition="{}", version=1, state="stopped", group_id="g-ana",
                                             created_by=ANA, created_ms=now, updated_ms=now))
    svc.policy.grants[ANA] = {"VIEW_NIFI_DEPLOYMENT"}
    with pytest.raises(NiFiServiceError, match="DEPLOY_NIFI_FLOW"):
        svc.set_running(ANA, "anas", True)
    with pytest.raises(NiFiServiceError, match="MANAGE_NIFI_DEPLOYMENT"):
        svc.set_running(BOB, "anas", False)                           # someone else's flow: the manager's


class _Board:
    """NiFi's bulletin board, shared by every flow on it, as NiFi 2 answers it (VolatileBulletinRepository): ``after``
    and the ``groupId`` pattern (searched for) filter, and ``limit`` cuts the matches component by component, here
    newest first: not oldest first, so a cursor moved past the highest id of a cut answer would skip the rest."""
    def __init__(self, bulletins):
        self.all = bulletins
        self.asked = []

    def bulletins(self, after=None, limit=None, groups=None):
        import re

        self.asked.append((after, limit, groups))
        got = [b for b in self.all if (after is None or b["id"] > after)
               and (groups is None or re.search(groups, b["groupId"]))]
        got = sorted(got, key=lambda b: -b["id"])[:limit] if limit else got
        return sorted(got, key=lambda b: b["id"])

    def status(self, gid):
        return {"id": gid}


def _running_flow(svc, name="anas", owner=ANA, gid="g-ana"):
    now = int(time.time() * 1000)
    with svc.engine.begin() as c:
        c.execute(insert(flows_table).values(name=name, deployment="ingest-dev", owner=owner, spec="{}",
                                             definition='{"flowContents": {}}', version=1, state="running",
                                             group_id=gid, created_by=owner, created_ms=now, updated_ms=now))


def test_a_busy_neighbour_does_not_hide_a_flows_errors(svc):
    """The bulletin board is NiFi-wide. NiFi is asked for this flow's groups only and for every match (no limit, which
    NiFi applies in no particular order): 600 bulletins of another flow, and many of this one after its error, never
    hide the error."""
    _running_flow(svc)
    board = [{"id": i, "groupId": "g-other", "bulletin": {"level": "WARNING", "message": "busy"}} for i in range(1, 601)]
    board.append({"id": 601, "groupId": "g-ana", "bulletin": {"level": "ERROR", "message": "the sink refused a row"}})
    board += [{"id": i, "groupId": "g-ana", "bulletin": {"level": "WARNING", "message": "slow"}}
              for i in range(602, 1300)]
    svc._clients["ingest-dev:u1"] = nifi = _Board(board)
    run = svc.sync("anas")
    assert run and run["state"] == "failed" and "refused a row" in json.dumps(run)
    assert nifi.asked == [(None, None, "^(?:g\\-ana)$")]
    assert svc._flow_row("anas").bulletin_cursor == 1299
    nifi.all.append({"id": 1300, "groupId": "g-ana", "bulletin": {"level": "ERROR", "message": "again"}})
    run = svc._flow_row("anas") and svc.sync("anas")
    assert run["state"] == "failed" and nifi.asked[-1][0] == 1299


def test_one_flows_failure_never_stops_the_others(svc, monkeypatch):
    _running_flow(svc, "anas", ANA, "g-ana")
    _running_flow(svc, "bobs", BOB, "g-bob")
    done = []

    def sync(name):
        if name == "anas":
            raise ValueError("its stored spec is not JSON")
        done.append(name)
    monkeypatch.setattr(svc, "sync", sync)
    monkeypatch.setattr(svc, "_rotate", lambda row: False)
    svc.run_due()
    assert done == ["bobs"]


def test_any_failure_in_a_deploy_leaves_nothing_half_made(svc):
    """An error that is not NiFi's (here the upload raises a plain ValueError, after the old group was removed) still
    removes the new group, records the flow as failed with the reason, and ends the old group's credential."""
    removed, ended = [], []
    svc.lakehouse_sessions = SimpleNamespace(end=lambda sid, actor: ended.append(sid))

    class Breaks:
        def find_group(self, name):
            return {"id": "g-old"}

        def remove(self, gid):
            removed.append(gid)

        def upload(self, *a, **kw):
            raise ValueError("NiFi answered something Cairn cannot read")
    svc._clients["ingest-dev:u1"] = Breaks()
    svc._check_sink = lambda owner, sink: None
    svc._lake_credential = lambda actor, row, spec: ("new-sid", {"cairn.lakehouse.token": "t"})
    flow = lake_flow()
    now = int(time.time() * 1000)
    spec = check_flow(flow)
    definition = spec.pop("definition", flow["definition"])
    with svc.engine.begin() as c:
        c.execute(insert(flows_table).values(name="contacts", deployment="ingest-dev", owner=ANA,
                                             spec=json.dumps(spec), definition=json.dumps(definition), version=2,
                                             state="running", group_id="g-old", lake_session="old-sid",
                                             created_by=ANA, created_ms=now, updated_ms=now))
    with pytest.raises(ValueError):
        svc.deploy(ANA, "contacts")
    row = svc._flow_row("contacts")
    assert row.state == "failed" and "cannot read" in row.error and row.group_id is None
    assert removed == ["g-old"] and sorted(ended) == ["new-sid", "old-sid"] and row.lake_session is None


def _services_answering(*rounds):
    """A NiFi client whose controller-service listing answers ``rounds`` in turn (the last one again after that)."""
    from cairn.nifi.api import NiFi

    nifi = NiFi.__new__(NiFi)
    seen = []

    def call(method, path, body=None, params=None, **kw):
        if method == "PUT":
            return {}
        seen.append(path)
        comps = rounds[min(len(seen) - 1, len(rounds) - 1)]
        return {"controllerServices": [{"component": c} for c in comps]}
    nifi.call = call
    return nifi


def test_services_wait_for_the_ones_they_reference_and_fail_on_anything_else(monkeypatch):
    """NiFi enables a group's services in reference order: meanwhile the catalog reports itself invalid because its
    file IO service is not enabled yet. That is waited for (CI run 385 failed the deploy on it); a service invalid for
    any other reason fails at once, and one still waiting when time is up is reported with why."""
    from cairn.nifi.api import NiFiError

    monkeypatch.setattr(time, "sleep", lambda s: None)
    waiting = "'File IO Provider' validated against 'io-1' is invalid because Controller Service with ID io-1 is disabled"
    catalog = {"id": "cat-1", "name": "Cairn Catalog", "state": "ENABLING", "validationStatus": "INVALID",
               "validationErrors": [waiting]}
    io = {"id": "io-1", "name": "Cairn Store", "state": "ENABLING", "validationStatus": "VALID"}
    done = [{**catalog, "state": "ENABLED", "validationStatus": "VALID", "validationErrors": []},
            {**io, "state": "ENABLED"}]
    _services_answering([catalog, io], [catalog, {**io, "state": "ENABLED"}], done).services("g", "ENABLED")
    bad = {**catalog, "validationErrors": ["'Catalog URI' is invalid because it is required"]}
    with pytest.raises(NiFiError, match="not valid: Cairn Catalog: 'Catalog URI'"):
        _services_answering([bad, io]).services("g", "ENABLED")
    with pytest.raises(NiFiError, match="not valid: Cairn Catalog: 'File IO Provider'"):
        _services_answering([catalog, io]).services("g", "ENABLED", timeout=-1)
    with pytest.raises(NiFiError, match=r"did not become enabled in time: Cairn Store \(ENABLING\)"):
        _services_answering([{**io, "id": "io-2"}, io]).services("g", "ENABLED", timeout=-1)


# ---- review fixes: what a flow's owner may make NiFi do ----------------------------------------------------------
def _two_flows(svc):
    now = int(time.time() * 1000)
    with svc.engine.begin() as c:
        for name, owner, gid in (("anas", ANA, "g-ana"), ("bobs", BOB, "g-bob")):
            c.execute(insert(flows_table).values(name=name, deployment="ingest-dev", owner=owner, spec="{}",
                                                 definition="{}", version=1, state="running", group_id=gid,
                                                 created_by=owner, created_ms=now, updated_ms=now))
    return _Canvas(parents={"g-ana": "root", "g-bob": "root", "g-ana-inner": "g-ana"},
                   components={("processors", "p-ana"): "g-ana-inner"})


def test_an_owner_cannot_point_their_flow_at_another_flows_parameter_context(svc):
    """Changing one's own group through the canvas is decided on the body too: binding it (or a new nested group, or
    pasted or uploaded content) to another flow's parameter context would hand its processors that flow's secrets
    and lakehouse token."""
    canvas = _two_flows(svc)
    row = svc._row("ingest-dev")
    change = lambda path, body, method="PUT": svc._may_change_part(  # noqa: E731
        ANA, row, canvas, method, path.split("/"), json.dumps(body).encode() if not isinstance(body, bytes) else body)
    own = {"component": {"id": "g-ana", "name": "cairn-anas", "parameterContext": {"id": "ctx-g-ana"}}}
    change("nifi-api/process-groups/g-ana", own)                         # her own context: fine
    change("nifi-api/processors/p-ana", {"component": {"config": {"properties": {"Batch Size": "#{size}"}}}})
    for body in ({"component": {"id": "g-ana", "parameterContext": {"id": "ctx-g-bob"}}},
                 {"component": {"name": "inner", "parameterContext": {"id": "ctx-g-bob"}}},
                 {"versionedFlowSnapshot": {"flowContents": {"parameterContextName": "cairn-bobs"}}},
                 {"copyResponse": {"parameterContexts": {"cairn-bobs": {"name": "cairn-bobs"}}}}):
        with pytest.raises(NiFiServiceError, match="its own parameter context") as e:
            change("nifi-api/process-groups/g-ana-inner/process-groups", body, "POST")
        assert e.value.status == 403
    with pytest.raises(NiFiServiceError, match="only JSON changes"):  # an upload: a definition NiFi would read
        change("nifi-api/process-groups/g-ana/process-groups/upload", b"--x\r\nContent-Disposition: form-data", "POST")
    svc.policy.grants[ANA] = {"MANAGE_NIFI_DEPLOYMENT"}                  # the deployment's manager decides NiFi-wide
    change("nifi-api/process-groups/g-ana", {"component": {"parameterContext": {"id": "ctx-g-bob"}}})


def test_nested_groups_are_bound_to_the_flows_own_context():
    """A nested group naming a context (another flow's, by name) is rebound to the flow's own: NiFi would otherwise
    bind it to the existing context of that name, with that flow's secrets."""
    d = template()
    d["flowContents"]["processGroups"] = [{"identifier": "inner", "name": "inner", "parameterContextName": "cairn-bobs",
                                           "processors": [], "processGroups": [
                                               {"identifier": "deep", "name": "deep",
                                                "parameterContextName": "cairn-bobs", "processors": []}]}]
    out = flowdef.prepare(flowdef.check_definition(d), "anas", {"sample.records": "[]"}, [],
                          {k: "x" for k in flowdef.LAKEHOUSE_PARAMS})
    inner = out["flowContents"]["processGroups"][0]
    assert out["flowContents"]["parameterContextName"] == inner["parameterContextName"] == "cairn-anas"
    assert inner["processGroups"][0]["parameterContextName"] == "cairn-anas"
    assert set(out["parameterContexts"]) == {"cairn-anas"}


def test_a_shared_deployment_runs_nothing_that_reaches_nifis_host(svc):
    """Commands, scripts, NiFi's own files and code loaded from a path or URL would read nifi.properties (the
    sensitive properties key) and with it every other flow's secrets: refused on a shared deployment, in a definition
    and through the canvas, unless the deployment is one team's (host_access)."""
    assert flowdef.host_access(template()) == []                         # the lakehouse template reaches nothing
    for t, props in (("org.apache.nifi.processors.standard.ExecuteStreamCommand", {"Command Path": "cat"}),
                     ("org.apache.nifi.processors.script.ExecuteScript", {"Script Body": "x"}),
                     ("org.apache.nifi.processors.standard.GetFile", {"Input Directory": "/opt/nifi/conf"}),
                     ("org.apache.nifi.lookup.script.ScriptedLookupService", {})):
        d = template()
        d["flowContents"]["processors"].append({"identifier": "x", "name": "x", "type": t, "properties": props})
        with pytest.raises(NiFiServiceError, match="is shared") as e:
            svc.create_flow(ANA, {"name": "hostly", **lake_flow(definition=d)})
        assert e.value.status == 403
    d = template()
    d["flowContents"]["controllerServices"].append({
        "identifier": "pool", "name": "pool", "type": "org.apache.nifi.dbcp.DBCPConnectionPool",
        "properties": {"Database Driver Location(s)": "https://evil.example.com/x.jar"}})
    assert flowdef.host_access(d) == ["pool.Database Driver Location(s)"]
    canvas = _two_flows(svc)
    with pytest.raises(NiFiServiceError, match="is shared"):
        svc._may_change_part(ANA, svc._row("ingest-dev"), canvas, "POST",
                             "nifi-api/process-groups/g-ana/processors".split("/"),
                             json.dumps({"component": {"type": "org.apache.nifi.processors.standard.ExecuteProcess"}})
                             .encode())
    # One team's NiFi may (and the flow then deploys as any other).
    with svc.engine.begin() as c:
        spec = {**json.loads(svc._row("ingest-dev").spec), "host_access": True}
        c.execute(deployments_table.update().values(spec=json.dumps(spec)))
    svc._check_host_access(svc._row("ingest-dev"), d)
    with pytest.raises(SpecError, match="host_access is true or false"):
        check_deployment({"host_access": "yes"}, None, V)


def test_only_the_owner_starts_a_flow_and_starting_checks_the_sink_again(svc):
    """A manager stops or removes someone's flow, never starts it (it lands data as the owner); an owner whose write
    access to the sink table was taken away after the deploy cannot start it again."""
    now = int(time.time() * 1000)
    sink = {"type": "lakehouse", "namespace": "crm", "table": "contacts"}
    with svc.engine.begin() as c:
        c.execute(insert(flows_table).values(name="anas", deployment="ingest-dev", owner=ANA,
                                             spec=json.dumps({"sink": sink}), definition=json.dumps(template()),
                                             version=1, state="stopped", group_id="g-ana", created_by=ANA,
                                             created_ms=now, updated_ms=now))
    scheduled = []
    svc._clients["ingest-dev:u1"] = SimpleNamespace(invalid=lambda gid: [],
                                                    schedule=lambda gid, st: scheduled.append(st))
    svc.policy.grants[BOB] = {"MANAGE_NIFI_DEPLOYMENT"}
    with pytest.raises(NiFiServiceError, match="only the flow's owner") as e:
        svc.set_running(BOB, "anas", True)
    assert e.value.status == 403
    svc.set_running(BOB, "anas", False)                               # stopping is the manager's
    svc.catalog_url, svc.store_url = "http://catalog:8181", "http://store:9000"
    svc.lakehouse_sessions = SimpleNamespace()
    svc.lakehouse = SimpleNamespace(decide=lambda who, tables: {"tables": [{"read": True, "write": False}]})
    with pytest.raises(NiFiServiceError, match="may not write crm.contacts"):
        svc.set_running(ANA, "anas", True)
    svc.lakehouse = SimpleNamespace(decide=lambda who, tables: {"tables": [{"read": True, "write": True}]})
    svc.set_running(ANA, "anas", True)
    assert scheduled == ["STOPPED", "RUNNING"]


def test_a_deploy_that_waited_for_another_acts_on_what_that_one_recorded(svc):
    """Two deploys at once: the second reads the flow once it holds the lock, so it ends the session the first one
    recorded (not the one both read before) and nothing stays live."""
    import threading

    now = int(time.time() * 1000)
    with svc.engine.begin() as c:
        c.execute(insert(flows_table).values(name="anas", deployment="ingest-dev", owner=ANA, spec=json.dumps(
            check_flow({"deployment": "ingest-dev", "definition": {"flowContents": {"processors": []}}}, None)),
            definition=json.dumps({"flowContents": {"processors": []}}),
            version=1, state="running", group_id="g-0", lake_session="s0", created_by=ANA,
            created_ms=now, updated_ms=now))
    ended, removed = [], []
    svc.lakehouse_sessions = SimpleNamespace(end=lambda sid, actor: ended.append(sid))

    class N:
        def find_group(self, name):
            return None

        def remove(self, gid):
            removed.append(gid)

        def upload(self, d, name):
            return {"id": "g-new", "component": {"parameterContext": {"id": "ctx"}}}

        def services(self, gid, state):
            pass

        def wait_valid(self, gid):
            return []

        def schedule(self, gid, st):
            pass
    svc._clients["ingest-dev:u1"] = N()
    lock = svc._locks.setdefault("anas", threading.Lock())
    lock.acquire()
    t = threading.Thread(target=svc.deploy, args=(ANA, "anas"))
    t.start()
    time.sleep(0.2)
    svc._set_flow("anas", lake_session="s1", group_id="g-1")            # what a deploy that ran first recorded
    lock.release()
    t.join(10)
    assert removed == ["g-1"] and ended == ["s1"]


def test_a_flow_whose_group_nifi_did_not_remove_is_kept(svc):
    """Deleting a flow whose group NiFi would not remove keeps the flow (failed, with why) instead of leaving a group
    running that nothing tracks; its session is ended all the same. A group already gone is no error."""
    from cairn.nifi.api import NiFiError

    _running_flow(svc)
    svc._set_flow("anas", lake_session="s1")
    ended = []
    svc.lakehouse_sessions = SimpleNamespace(end=lambda sid, actor: ended.append(sid))

    def remove(gid):
        raise NiFiError("NiFi: DELETE: 409 the group is running", 409)
    svc._clients["ingest-dev:u1"] = SimpleNamespace(remove=remove)
    with pytest.raises(NiFiServiceError, match="was not removed from NiFi") as e:
        svc.delete_flow(ANA, "anas")
    assert e.value.status == 502 and ended == ["s1"]
    row = svc._flow_row("anas")
    assert row.state == "failed" and "delete it again" in row.error and row.lake_session is None

    def gone(gid):
        raise NiFiError("NiFi: DELETE: 404", 404)
    svc._clients["ingest-dev:u1"] = SimpleNamespace(remove=gone)
    svc.delete_flow(ANA, "anas")
    with pytest.raises(NiFiServiceError):
        svc._flow_row("anas")


def test_ports_recorded_for_deployments_are_not_handed_out_again(monkeypatch):
    from cairn.nifi.docker_runtime import DockerRuntime

    rt = DockerRuntime.__new__(DockerRuntime)
    rt.ports = (18443, 18446)
    monkeypatch.setattr(rt, "_taken_ports", lambda: {18443})
    assert rt.allocate_port() == 18444
    assert rt.allocate_port({18444, 18445}) == 18446                  # recorded, container gone or not made yet


def test_the_nifi_client_pools_connections_and_leaves_no_files(tmp_path, monkeypatch):
    import ssl
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from cairn.nifi.api import NiFi

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ca = x509.load_pem_x509_certificate(KEY.tls("d", "u", ["localhost"])["ca_pem"])
    c = NiFi("https://127.0.0.1:1", "pw", ca.public_bytes(serialization.Encoding.PEM))
    assert isinstance(c.verify, ssl.SSLContext) and c.http is c.http
    assert list(tmp_path.iterdir()) == []
    c.close()


def test_the_nifi_client_never_sends_back_a_cookie_nifi_set():
    """NiFi sets its request-token cookie at sign-in; a pooled client that kept it would send it with Cairn's writes,
    and NiFi then refuses them (403) without the matching CSRF header, as it did in CI run 388. Cairn signs in with
    a bearer token only."""
    import httpx
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from cairn.nifi.api import NiFi

    seen = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access/token"):
            return httpx.Response(201, text="tok", headers={
                "set-cookie": "__Secure-Request-Token=abc; Path=/; Secure; HttpOnly"})
        seen.append((req.method, req.headers.get("cookie"), req.headers.get("authorization")))
        return httpx.Response(201, json={"id": "g"})

    ca = x509.load_pem_x509_certificate(KEY.tls("d", "u", ["localhost"])["ca_pem"])
    c = NiFi("https://127.0.0.1:1", "pw", ca.public_bytes(serialization.Encoding.PEM))
    c._client._transport = httpx.MockTransport(handle)
    c.call("POST", "/process-groups/root/process-groups/upload", data={"groupName": "x"})
    c.call("PUT", "/process-groups/g", body={})
    assert seen == [("POST", None, "Bearer tok"), ("PUT", None, "Bearer tok")]
    c.close()
