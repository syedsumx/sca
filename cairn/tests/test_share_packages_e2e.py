"""AI data packages and agents on shared data (L117), end to end: a provider shares a lakehouse table, publishes the
share's data package (tables, columns, glossary, metrics, sample questions) and attaches a deployed agent app; the
recipient, with nothing but the share's credential, fetches the package and checks Cairn's signature against the
published JWKS, and asks the agent, which reads the shared table through Cairn Catalog with the share's identity and is
refused any other. Revoking the share stops both. Through the real services, decision point, database and HTTP API,
the real Cairn Store and Cairn Catalog, the agent's real code in its own process, and the model behind Cairn's AI
gateway (a local stand-in for the provider).

Needs the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

import cairn as cairn_pkg
from cryptography.exceptions import InvalidSignature

pytest.importorskip("pyiceberg")
pytest.importorskip("anthropic")

from test_ai_gateway import SONNET, Provider  # noqa: E402
from test_spark_e2e import lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import KEY, _ready, port, wait  # noqa: E402

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
ANA, BOB, ADMIN = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:admin"
SKU = "urn:li:glossaryTerm:Sku"
AGENT = '''
import json

MODEL = "claude-sonnet-5-5"


def respond(ctx, messages):
    """Reads the table the message names, through the run's catalog access, and has the model sum it up."""
    table = messages[-1]["content"].strip()
    try:
        rows = ctx.catalog().load_table(table).scan().to_arrow().to_pylist()
    except Exception as e:  # noqa: BLE001
        return f"refused {table}: {type(e).__name__}"
    msg = ctx.anthropic().messages.create(model=MODEL, max_tokens=50, messages=[
        {"role": "user", "content": json.dumps(rows, default=str)}])
    return f"{len(rows)} rows of {table} ({ctx.identity}); model: {msg.content[0].text}"
'''


@pytest.fixture(scope="module")
def cairn(tmp_path_factory):
    """The Cairn API with the lakehouse, agent apps (process runtime) and the AI gateway, in a thread."""
    import http.server

    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("packages")
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    p = port()
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY, "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg",
                                                                                       "database": ""}}),
           "CAIRN_AI_GATEWAY_ANTHROPIC_URL": f"http://127.0.0.1:{up.server_address[1]}",
           "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream", "CAIRN_AI_GATEWAY_MODELS": SONNET,
           "CAIRN_AGENT_APPS_RUNTIME": "process", "CAIRN_AGENT_APPS_DIR": str(tmp / "apps"),
           "CAIRN_AGENT_APPS_CAIRN_URL": f"http://127.0.0.1:{p}"}
    saved = {k: os.environ.get(k) for k in [*env, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]}
    os.environ.update(env)
    # The agent's own process runs this checkout's agent runtime (whatever cairn the environment has installed).
    src = str(Path(cairn_pkg.__file__).resolve().parents[1])
    old_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = src + (os.pathsep + old_path if old_path else "")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        os.environ.pop(k, None)
    try:
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="builders", effect="allow", actions=["BUILD_AGENTS"], condition=sub(ANA)),
        Policy(id="models", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{SONNET}"]), condition=sub(ANA))]))
    Provider.seen.clear()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield f"http://127.0.0.1:{p}", s
    if s.agent_apps is not None:
        for name in list(s.agent_apps.runtime.procs):
            s.agent_apps.runtime.stop(name)
    server.should_exit = True
    up.shutdown()
    if old_path is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = old_path


def api(url, who, method, path, **kw):
    return httpx.request(method, f"{url}{path}", headers={"X-Cairn-Actor": who}, timeout=120, **kw)


def verify_jws(jws: str, jwks: dict) -> dict:
    """The recipient's own check of Cairn's signature (RS256 against the published key), without Cairn's code."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    def unb64(x):
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    h, c, sig = jws.split(".")
    header = json.loads(unb64(h))
    [key] = [k for k in jwks["keys"] if k["kid"] == header["kid"]]
    pub = rsa.RSAPublicNumbers(int.from_bytes(unb64(key["e"]), "big"), int.from_bytes(unb64(key["n"]), "big")).public_key()
    pub.verify(unb64(sig), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())   # raises when it is not
    assert header["typ"] == "cairn-data-package+jws" and header["alg"] == "RS256"
    return json.loads(unb64(c))


def test_a_recipient_gets_a_signed_package_and_asks_an_agent_over_shared_tables_only(cairn, lake, tmp_path):  # noqa: F811
    from sqlalchemy import insert, select

    from cairn.agentkit.project import deploy, init
    from cairn.identity.agents import agent_tokens
    from cairn.sqlworkspace.metrics import metrics

    url, s = cairn
    s.shares.catalog_url, s.shares.token_url = lake["catalog"], f"{url}/v1/shares/oauth/tokens"
    products = s.lakehouse.urn(["analytics"], "products")
    # The provider's semantic context in the catalog and the metrics layer.
    s.graph.aspects[(products, "datasetProperties")] = {"description": "Every product we sell, with its list price."}
    s.graph.aspects[(products, "schemaMetadata")] = {"fields": [
        {"fieldPath": "sku", "nativeDataType": "string", "description": "Stock keeping unit",
         "glossaryTerms": {"terms": [{"urn": SKU}]}},
        {"fieldPath": "price", "nativeDataType": "bigint", "description": "List price in cents"}]}
    s.graph.aspects[(SKU, "glossaryTermInfo")] = {"name": "SKU", "definition": "A product's unique stock code."}
    now = int(time.time() * 1000)
    with s.engine.begin() as c:
        for name, table in (("avg_price", "analytics.products"), ("order_count", "sales.orders")):
            c.execute(insert(metrics).values(id=uuid.uuid4().hex, name=name, label=name.replace("_", " "),
                                             description=f"The {name}", connection="lakehouse", table_name=table,
                                             measure="Avg([price])" if name == "avg_price" else "Count()",
                                             filters="[]", time_column=None, dimensions="[]", synonyms="[]",
                                             owner=ANA, certified=True, version=1, updated_ms=now))

    # The share, and the provider's agent app attached to it.
    share = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-a", "recipient": "Partner A",
                                                      "tables": ["analytics.products"], "expires_days": 7})
    assert share.status_code == 200, share.text
    share = share.json()
    proj = init(str(tmp_path / "partner-helper"), template="basic")
    (proj / "agent.py").write_text(AGENT)
    (proj / "requirements.txt").write_text("anthropic\npyiceberg\n")
    app = deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    assert app["status"] == "running"
    assert api(url, BOB, "POST", "/v1/shares/partner-a/agents", json={"app": "partner-helper"}).status_code == 404
    att = api(url, ANA, "POST", "/v1/shares/partner-a/agents", json={"app": "partner-helper"})
    assert att.status_code == 200 and [a["app"] for a in att.json()["agents"]] == ["partner-helper"], att.text

    # The data package: only what is shared is described.
    bad = api(url, ANA, "PUT", "/v1/shares/partner-a/package", json={"metrics": ["order_count"]})
    assert bad.status_code == 409 and "does not include" in bad.text
    pub = api(url, ANA, "PUT", "/v1/shares/partner-a/package", json={
        "metrics": ["avg_price"], "sample_questions": ["What is our average list price?"],
        "notes": "Prices are refreshed nightly."})
    assert pub.status_code == 200 and pub.json()["version"] == 1, pub.text
    assert api(url, BOB, "GET", "/v1/shares/partner-a/package").status_code == 404
    [listed] = api(url, ANA, "GET", "/v1/share-packages").json()["packages"]
    assert (listed["share"], listed["version"], listed["metrics"], listed["agents"]) == \
        ("partner-a", 1, ["avg_price"], ["partner-helper"])
    assert api(url, BOB, "GET", "/v1/share-packages").json()["packages"] == []

    # The recipient: a token from the share's credential, the package, and Cairn's signature checked against the JWKS.
    sid, secret = share["credential"].split(":", 1)
    tok = httpx.post(f"{url}/v1/shares/oauth/tokens", auth=(sid, secret), data={"grant_type": "client_credentials"})
    bearer = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    got = httpx.get(f"{url}/v1/shares/recipient/package", headers=bearer)
    assert got.status_code == 200, got.text
    pkg = got.json()
    jwks = httpx.get(f"{url}{pkg['jwks_uri']}").json()
    manifest = verify_jws(pkg["signature"], jwks)
    assert manifest == pkg["manifest"] and manifest["version"] == 1
    [t] = manifest["tables"]
    assert t["name"] == "analytics.products" and t["description"].startswith("Every product")
    assert t["columns"][0] == {"name": "sku", "type": "string", "description": "Stock keeping unit",
                               "glossary_terms": [SKU]}
    assert manifest["glossary"] == [{"urn": SKU, "name": "SKU", "definition": "A product's unique stock code."}]
    assert [m["name"] for m in manifest["metrics"]] == ["avg_price"] and manifest["metrics"][0]["certified"]
    assert manifest["sample_questions"] == ["What is our average list price?"]
    assert manifest["agents"][0]["name"] == "partner-helper" and manifest["catalog"]["uri"] == lake["catalog"]
    tampered = pkg["signature"].split(".")
    tampered[1] = base64.urlsafe_b64encode(json.dumps({**manifest, "tables": []}).encode()).decode().rstrip("=")
    with pytest.raises(InvalidSignature):
        verify_jws(".".join(tampered), jwks)
    # The credential itself works too; anything else does not.
    basic = httpx.get(f"{url}/v1/shares/recipient/package", auth=(sid, secret))
    assert basic.status_code == 200 and basic.json()["digest"] == pkg["digest"]
    assert httpx.get(f"{url}/v1/shares/recipient/package", auth=(sid, "wrong")).status_code == 401
    assert httpx.get(f"{url}/v1/shares/recipient/package", headers={"X-Cairn-Actor": ANA}).status_code == 401
    ana_token = s.trino_tokens.mint(ANA, 300, audience="lakehouse")["token"]
    assert httpx.get(f"{url}/v1/shares/recipient/package",
                     headers={"Authorization": f"Bearer {ana_token}"}).status_code == 401

    # The recipient asks the attached agent: the shared table is read, any other refused.
    assert [a["name"] for a in httpx.get(f"{url}/v1/shares/recipient/agents", headers=bearer).json()["agents"]] == \
        ["partner-helper"]

    def ask(text):
        return httpx.post(f"{url}/v1/shares/recipient/agents/partner-helper/invocations", headers=bearer,
                          json={"input": text, "session_id": "p1"}, timeout=180)
    ok = ask("analytics.products")
    assert ok.status_code == 200, ok.text
    assert ok.json()["output"] == "3 rows of analytics.products (share-recipient); model: Hello from the model"
    for other in ("sales.orders", "hr.salaries"):
        r = ask(other)
        assert r.status_code == 200 and r.json()["output"].startswith(f"refused {other}"), r.text
    assert "sku" in Provider.seen[-1]["body"]["messages"][0]["content"]      # the model saw the shared rows
    # Metered to the recipient (as the agent), allowed by the provider who attached it; every run token revoked
    # and narrowed to the model scope, with the recipient's identity type.
    recipient = f"urn:li:corpuser:share:{sid}"
    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        rows = c.execute(select(usage_table).where(usage_table.c.actor == recipient)).all()
        toks = c.execute(select(agent_tokens).where(agent_tokens.c.client_id == app["agent_id"])).all()
    assert rows and {r.task for r in rows} == {f"gateway:{app['agent_id']}"}
    assert toks and all(t.revoked and t.scopes == ["model"] and t.actor == recipient for t in toks)
    assert all(t.details["identity"] == "share-recipient" and t.details["sponsor"] == ANA for t in toks)
    # Such a token reaches nothing in Cairn but the AI gateway.
    held = s.agents.issue_run_token(app["agent_id"], recipient, scopes=["model"], identity="share-recipient",
                                    sponsor=ANA)
    q = httpx.post(f"{url}/data/query", json={"sql": "SELECT 1", "connection": "x"},
                   headers={"Authorization": f"Bearer {held}"})
    assert q.status_code == 403 and "query_data" in q.text
    assert httpx.get(f"{url}/v1/shares", headers={"Authorization": f"Bearer {held}"}).status_code == 403
    s.agents.revoke_token(held)
    usage = api(url, ANA, "GET", "/v1/shares/partner-a/agents").json()
    assert usage["recipient_urn"] == recipient and usage["agents"][0]["usage"]["runs"] == 3
    audit = [e for e in s.audit.entries(category="shares", limit=200)]
    acts = {e["action"] for e in audit}
    assert {"share.package_published", "share.package_fetched", "share.agent_attached", "share.agent_invoked"} <= acts
    invoked = [e for e in audit if e["action"] == "share.agent_invoked"][-1]
    assert invoked["actor"] == recipient and invoked["details"]["identity"] == "share-recipient"

    # Revoking the share stops the package and the agent at once.
    lake_token = s.trino_tokens.mint(recipient, 600, audience="lakehouse", extra={"share": sid})["token"]
    assert api(url, ANA, "POST", f"/v1/shares/{sid}/revoke").json()["state"] == "revoked"
    assert httpx.get(f"{url}/v1/shares/recipient/package", headers=bearer).status_code == 403
    assert httpx.get(f"{url}/v1/shares/recipient/package", auth=(sid, secret)).status_code == 403
    assert ask("analytics.products").status_code == 403
    r = httpx.get(f"{lake['catalog']}/v1/namespaces/analytics/tables/products",
                  headers={"Authorization": f"Bearer {lake_token}"}, timeout=10)
    assert r.status_code == 401                          # what the agent held no longer reads anything
    assert api(url, ANA, "PUT", "/v1/shares/partner-a/package", json={}).status_code == 409
