"""Live end-to-end tests for the Active Metadata layer (module E) against the compose stack.

Run after ``test_stack_smoke.py``'s prerequisites (``DHE_E2E=1``, ``DHE_E2E_TOKEN`` = root PAT). Also needs
``DHE_E2E_SLACK_SECRET`` (= ``DHE_SLACK_SIGNING_SECRET`` from ``.env``) for the Slack scenario and
``DHE_E2E_SINK`` (URL of an HTTP sink that records POSTs, reachable from dhe-notifier) for webhooks.

Each test acts as real DataHub users: ``bob`` (analyst, restricted) and ``carol`` (data owner / approver),
created with their own personal access tokens, so authorization is exercised end to end through
GMS, the DHE PDP, the masking proxy and dhe-api.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.parse
import uuid

import httpx
import pytest

from test_stack_smoke import API, EDGE, GMS, TOKEN, emit, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("DHE_E2E") != "1", reason="set DHE_E2E=1 to run against a live stack")

FRONTEND = os.environ.get("DHE_E2E_FRONTEND", "http://localhost:9002")
SLACK_SECRET = os.environ.get("DHE_E2E_SLACK_SECRET", "")
SINK = os.environ.get("DHE_E2E_SINK", "")            # as seen from inside the compose network
SINK_HOST = os.environ.get("DHE_E2E_SINK_HOST", "")  # same sink, as seen from the test runner
COMPOSE = ["docker", "compose", "-f", os.environ.get("DHE_E2E_COMPOSE", "docker-compose.enterprise.yml")]

RUN = uuid.uuid4().hex[:8]
ROOT = "urn:li:corpuser:datahub"
BOB = f"urn:li:corpuser:e2e_bob_{RUN}"
CAROL = f"urn:li:corpuser:e2e_carol_{RUN}"
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,am_{RUN}.public.customers,PROD)"
DS_DOWN = f"urn:li:dataset:(urn:li:dataPlatform:snowflake,am_{RUN}.analytics.customer_orders,PROD)"
PII_EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
GDPR_TAG = "urn:li:tag:gdpr-subject"
DOMAIN = f"urn:li:domain:am-{RUN}"
PRODUCT = f"urn:li:dataProduct:am-{RUN}"
STAMP = {"time": 0, "actor": ROOT}


# --------------------------------------------------------------------------- helpers
def mint_token(actor: str) -> str:
    data = gql("""mutation($input: CreateAccessTokenInput!) { createAccessToken(input: $input) { accessToken } }""",
               {"input": {"type": "PERSONAL", "actorUrn": actor, "duration": "ONE_DAY", "name": f"e2e {RUN}"}})
    return data["createAccessToken"]["accessToken"]


def client(token: str, base: str = API) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def frontend_gql(token: str, query: str, variables: dict | None = None) -> httpx.Response:
    """GraphQL through datahub-frontend, i.e. frontend → dhe-gms-proxy → GMS (the masking path)."""
    return httpx.post(f"{FRONTEND}/api/v2/graphql", json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {token}"}, timeout=60)


def compose(*args: str, timeout: int = 300) -> str:
    out = subprocess.run(COMPOSE + list(args), capture_output=True, text=True, timeout=timeout,
                         cwd=os.path.join(os.path.dirname(__file__), "..", ".."))
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def slack_post(path: str, form: dict) -> httpx.Response:
    body = urllib.parse.urlencode(form).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SLACK_SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return httpx.post(f"{API}{path}", content=body, timeout=30, headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded"})


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def users():
    for urn, name in ((BOB, "Bob E2E"), (CAROL, "Carol E2E")):
        emit(urn, "corpUserInfo", {"active": True, "displayName": name, "email": f"{urn.split(':')[-1]}@example.com"})
    # Carol is a data steward (the base bundle masks PII for everyone else) and holds the support purpose.
    emit(CAROL, "groupMembership", {"groups": ["urn:li:corpGroup:data-stewards"]})
    tokens = {"bob": mint_token(BOB), "carol": mint_token(CAROL), "root": TOKEN}
    with client(tokens["root"]) as root, client(tokens["bob"]) as bob, client(tokens["carol"]) as carol:
        yield {"root": root, "bob": bob, "carol": carol, "tokens": tokens}
        # Purposes apply platform-wide (every PII column), so never leave this run's behind.
        for kind in ("personas", "purposes"):
            for item in root.get(f"/governance/{kind}").json():
                if item["id"].endswith(RUN):
                    root.delete(f"/governance/{kind}/{item['id']}")


@pytest.fixture(scope="module", autouse=True)
def seed(users):
    fields = [{"fieldPath": p, "nativeDataType": t, "type": {"type": {"com.linkedin.schema.StringType": {}}}}
              for p, t in (("id", "INT"), ("email", "VARCHAR"), ("amount", "NUMERIC"), ("region", "VARCHAR"))]
    schema = {"schemaName": "customers", "platform": "urn:li:dataPlatform:postgres", "version": 0, "hash": "",
              "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}}, "fields": fields}
    emit(DS, "schemaMetadata", schema)
    emit(DS, "datasetProperties", {"name": "customers", "description": "E2E customers with contact details."})
    emit(DS, "ownership", {"owners": [{"owner": CAROL, "type": "DATAOWNER"}], "lastModified": STAMP})
    emit(DS, "datasetProfile", {"timestampMillis": int(time.time() * 1000), "rowCount": 1200, "columnCount": 4,
                                "fieldProfiles": [
                                    {"fieldPath": "email", "sampleValues": ["jane.doe@example.com", "li@example.org"]},
                                    {"fieldPath": "region", "sampleValues": ["EMEA", "APAC"]}]})
    emit(DS_DOWN, "schemaMetadata", {**schema, "schemaName": "customer_orders", "platform": "urn:li:dataPlatform:snowflake",
                                     "fields": [{**fields[1], "fieldPath": "customer_email"}, fields[2]]})
    emit(DS_DOWN, "datasetProperties", {"name": "customer_orders", "description": "E2E downstream mart."})
    emit(DS_DOWN, "upstreamLineage", {"upstreams": [{"dataset": DS, "type": "TRANSFORMED", "auditStamp": STAMP}],
                                      "fineGrainedLineages": [{
                                          "upstreamType": "FIELD_SET", "upstreams": [f"urn:li:schemaField:({DS},email)"],
                                          "downstreamType": "FIELD",
                                          "downstreams": [f"urn:li:schemaField:({DS_DOWN},customer_email)"],
                                          "confidenceScore": 1.0}]})
    # Label the PII column: a classification term (PII propagation + purposes) and a GDPR tag (rule propagation).
    emit(DS, "editableSchemaMetadata", {"created": STAMP, "lastModified": STAMP, "editableSchemaFieldInfo": [{
        "fieldPath": "email", "glossaryTerms": {"terms": [{"urn": PII_EMAIL}], "auditStamp": STAMP},
        "globalTags": {"tags": [{"tag": GDPR_TAG}]}}]})
    # Domain + data product containing DS (for persona scope and the marketplace).
    gql("""mutation($input: CreateDomainInput!) { createDomain(input: $input) }""",
        {"input": {"id": f"am-{RUN}", "name": f"AM E2E {RUN}"}})
    emit(DS, "domains", {"domains": [DOMAIN]})
    gql("""mutation($input: CreateDataProductInput!) { createDataProduct(input: $input) { urn } }""",
        {"input": {"id": f"am-{RUN}", "domainUrn": DOMAIN,
                   "properties": {"name": f"Customer 360 {RUN}", "description": "E2E data product"}}})
    gql("""mutation($input: BatchSetDataProductInput!) { batchSetDataProduct(input: $input) }""",
        {"input": {"dataProductUrn": PRODUCT, "resourceUrns": [DS, DS_DOWN]}})
    emit(PRODUCT, "ownership", {"owners": [{"owner": CAROL, "type": "DATAOWNER"}], "lastModified": STAMP})


# --------------------------------------------------------------------------- 1. platform surface
def test_01_services_and_ui_assets_are_served(users):
    for url in (f"{API}/health", f"{EDGE}/dhe/api/health", f"{EDGE}/dhe-ui/remoteEntry.js",
                f"{EDGE}/dhe-ui/embed/sdk.js", f"{EDGE}/dhe/api/embed/v1/sdk.js"):
        r = httpx.get(url, timeout=15, follow_redirects=True)
        assert r.status_code == 200, (url, r.status_code)
    assert "dheConsumer" in httpx.get(f"{EDGE}/dhe-ui/remoteEntry.js", timeout=15).text
    cfg = users["bob"].get("/ui/config").json()
    assert cfg["user"]["urn"] == BOB and cfg["features"]["marketplace"] and cfg["slack_enabled"]
    assert cfg["features"]["masking_proxy"] and cfg["features"]["embeds"]
    assert not users["bob"].get("/ui/config").json()["is_admin"]


# --------------------------------------------------------------------------- 2. personas & purposes
def test_02_personas_grant_and_purposes_deny_and_mask(users):
    root, bob = users["root"], users["bob"]
    p = root.post("/governance/personas", json={
        "name": f"Analysts {RUN}", "members": {"users": [BOB, CAROL]}, "scope": {"domains": [DOMAIN]},
        "permissions": ["view", "view_samples", "view_queries", "comment", "request_access"]})
    assert p.status_code == 201, p.text
    assert bob.post("/governance/personas", json={"name": "nope"}).status_code == 403   # admins only
    pu = root.post("/governance/purposes", json={
        "name": f"Customer support {RUN}", "tags": ["PII"], "allowed": {"users": [CAROL]},
        "deny": ["view_queries"], "mask": {"enabled": True, "strategy": "partial"}})
    assert pu.status_code == 201, pu.text
    assert [x["id"] for x in bob.get("/ui/config").json()["personas"]] == [p.json()["id"]]

    def decide(actor, privilege, urn=DS):
        return root.post("/governance/simulate", json={"actor": actor, "privilege": privilege,
                                                         "resource_urn": urn}).json()

    # Persona grant applies inside its scope only.
    assert decide(BOB, "VIEW_ENTITY_PAGE")["decision"] == "ALLOW"
    # Purpose: strict deny beats the persona grant for non-purpose users; the purpose user is unaffected.
    d = decide(BOB, "VIEW_ENTITY_QUERIES")
    assert d["decision"] == "DENY" and any(x.startswith("purpose:") for x in d["policy_ids"]), d
    assert decide(CAROL, "VIEW_ENTITY_QUERIES")["decision"] == "ALLOW"
    # Samples stay visible but carry a masking obligation for bob only.
    bob_samples, carol_samples = decide(BOB, "VIEW_DATASET_PROFILE"), decide(CAROL, "VIEW_DATASET_PROFILE")
    assert bob_samples["decision"] == "ALLOW" and any(o["type"] == "mask_columns" for o in bob_samples["obligations"])
    assert not carol_samples.get("obligations")


# --------------------------------------------------------------------------- 3. masking proxy
PROFILE_Q = """query($urn: String!) { dataset(urn: $urn) { urn
  datasetProfiles(limit: 1) { fieldProfiles { fieldPath sampleValues } } } }"""


def _samples(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text[:500]
    profiles = ((resp.json().get("data") or {}).get("dataset") or {}).get("datasetProfiles") or []
    return {f["fieldPath"]: f.get("sampleValues") for f in (profiles[0]["fieldProfiles"] if profiles else [])}


def test_03_masking_proxy_masks_pii_per_caller(users):
    tokens = users["tokens"]

    def masked_by_purpose():  # the proxy picks up new purposes within its reload interval
        r = frontend_gql(tokens["bob"], PROFILE_Q, {"urn": DS})
        return r if "purpose:" in r.headers.get("X-DHE-Masking-Policies", "") and _samples(r).get("email") else None

    r = eventually(masked_by_purpose, timeout=90, interval=3)
    bob = _samples(r)
    assert bob["email"] == ["j•••@•••.com", "l•••@•••.org"], bob          # partial mask
    assert bob["region"] == ["EMEA", "APAC"]                                # unlabelled column untouched
    assert int(r.headers["X-DHE-Masked"]) >= 1
    assert "mask-pii-for-non-stewards" in r.headers["X-DHE-Masking-Policies"]   # base bundle rule, by id
    carol = _samples(frontend_gql(tokens["carol"], PROFILE_Q, {"urn": DS}))
    assert carol["email"] == ["jane.doe@example.com", "li@example.org"]  # purpose holder sees values
    # Direct GMS access (service path) is not masked; only the frontend path goes through the proxy.
    direct = httpx.post(f"{GMS}/api/graphql", json={"query": PROFILE_Q, "variables": {"urn": DS}},
                        headers={"Authorization": f"Bearer {tokens['root']}"}, timeout=30)
    assert _samples(direct)["email"][0] == "jane.doe@example.com"


# --------------------------------------------------------------------------- 4. propagation
def _field_labels(urn: str, path: str) -> tuple[set, set]:
    r = httpx.get(f"{GMS}/aspects/{urllib.parse.quote(urn, safe='')}?aspect=editableSchemaMetadata&version=0",
                  headers={"Authorization": f"Bearer {TOKEN}", "X-RestLi-Protocol-Version": "2.0.0"}, timeout=30)
    if r.status_code != 200:
        return set(), set()
    esm = r.json()["aspect"]["com.linkedin.schema.EditableSchemaMetadata"]
    for f in esm.get("editableSchemaFieldInfo", []):
        if f["fieldPath"] == path:
            return ({t["urn"] for t in (f.get("glossaryTerms") or {}).get("terms", [])},
                    {t["tag"] for t in (f.get("globalTags") or {}).get("tags", [])})
    return set(), set()


def test_04_pii_terms_and_rule_based_tags_propagate_over_column_lineage():
    def propagated():
        terms, tags = _field_labels(DS_DOWN, "customer_email")
        return (terms, tags) if PII_EMAIL in terms and GDPR_TAG in tags else None
    terms, tags = eventually(propagated, timeout=180, interval=5)
    assert PII_EMAIL in terms and GDPR_TAG in tags


# --------------------------------------------------------------------------- 5. collaboration
def test_05_bookmarks_follows_discussions_and_trending(users):
    bob, carol = users["bob"], users["carol"]
    enc = urllib.parse.quote(DS, safe="")
    assert bob.put(f"/collab/bookmarks/{enc}", json={"note": "e2e"}).status_code in (200, 201)
    assert bob.put(f"/collab/follows/{enc}", json={}).status_code in (200, 201)
    for c in (bob, carol):
        assert c.post("/collab/views", json={"urn": DS}).status_code in (200, 204)
    t = bob.post("/collab/threads", json={"entity_urn": DS, "field_path": "email",
                                          "body_md": f"@e2e_carol_{RUN} is email verified at signup?"})
    assert t.status_code == 201, t.text
    tid = t.json()["id"]
    assert any(n["type"] == "discussion.mention" for n in carol.get("/notify/inbox").json())
    reply = carol.post(f"/collab/threads/{tid}/comments", json={"body_md": "Yes, double opt-in."})
    assert reply.status_code in (200, 201), reply.text
    assert any(n["type"] == "discussion.reply" for n in bob.get("/notify/inbox").json())
    cid = reply.json()["comments"][-1]["id"] if "comments" in reply.json() else reply.json()["id"]
    assert bob.post(f"/collab/comments/{cid}/reactions", json={"emoji": "👍"}).json()["active"] is True
    assert bob.request("DELETE", f"/collab/comments/{cid}/reactions", json={"emoji": "👍"}).json()["active"] is False
    assert carol.post(f"/collab/threads/{tid}/resolve").json()["status"] == "RESOLVED"
    trending = bob.get("/collab/trending", params={"limit": 50}).json()
    assert any(x["urn"] == DS and x["unique_viewers_7d"] >= 2 for x in trending)
    e = bob.post("/collab/enrich", json={"urns": [DS]}).json()[DS]
    assert e["bookmarked"] and e["following"]


# --------------------------------------------------------------------------- 6. marketplace + Slack
def test_06_marketplace_access_request_approved_from_slack(users):
    root, bob, carol = users["root"], users["bob"], users["carol"]
    enc = urllib.parse.quote(PRODUCT, safe="")
    pol = root.put(f"/marketplace/products/{enc}/policy", json={
        "approvers": [CAROL], "max_duration_days": 30, "purposes": ["Churn analysis", "Other"],
        "requires_justification": True, "grant_permissions": ["view", "view_samples"]})
    assert pol.status_code == 200, pol.text
    cards = eventually(lambda: [c for c in bob.get("/marketplace/products", params={"q": RUN}).json()
                                if c["urn"] == PRODUCT], timeout=90)
    assert cards[0]["access"]["status"] == "NONE"
    assert bob.post("/marketplace/access-requests", json={
        "product_urn": PRODUCT, "purpose": "Churn analysis", "duration_days": 90,
        "justification": "Quarterly churn model refresh"}).status_code == 422         # over max duration
    r = bob.post("/marketplace/access-requests", json={
        "product_urn": PRODUCT, "purpose": "Churn analysis", "duration_days": 14,
        "justification": "Quarterly churn model refresh"})
    assert r.status_code in (200, 201), r.text
    req = r.json()
    assert req["state"] == "PENDING" and req["approvers"] == [CAROL]
    assert bob.post(f"/marketplace/access-requests/{req['id']}/approve", json={}).status_code == 403   # SoD
    assert any(n["type"] == "access.requested" for n in carol.get("/notify/inbox").json())

    # Carol approves from Slack: link her Slack user id, then send a signed block_actions payload.
    settings = carol.get("/notify/settings").json()
    settings["channels"]["slack"] = {"enabled": True, "user_id": f"U{RUN.upper()}"}
    assert carol.put("/notify/settings", json=settings).status_code == 200
    bad = slack_post("/integrations/slack/interactions", {"payload": "{}"})
    bad_sig = httpx.post(f"{API}/integrations/slack/interactions", content=b"payload=%7B%7D", headers={
        "X-Slack-Request-Timestamp": str(int(time.time())), "X-Slack-Signature": "v0=forged"})
    assert bad.status_code == 200 and bad_sig.status_code == 401
    payload = {"type": "block_actions", "user": {"id": f"U{RUN.upper()}"},
               "actions": [{"action_id": "dhe:access_request:approve", "value": req["id"]}]}
    out = slack_post("/integrations/slack/interactions", {"payload": json.dumps(payload)})
    assert out.status_code == 200 and out.json()["text"].startswith("✅"), out.text
    assert bob.get(f"/marketplace/access-requests/{req['id']}").json()["state"] == "APPROVED"
    assert any(n["type"] == "access.granted" for n in bob.get("/notify/inbox").json())
    card = [c for c in bob.get("/marketplace/products", params={"q": RUN}).json() if c["urn"] == PRODUCT][0]
    assert card["access"]["status"] == "GRANTED"

    # Slash command search runs as the linked DataHub user.
    cmd = slack_post("/integrations/slack/commands", {"command": "/datahub", "text": "search customers",
                                                       "user_id": f"U{RUN.upper()}"})
    assert cmd.status_code == 200 and "text" in cmd.json()

    # Revoke ends the time-boxed persona grant.
    assert carol.post(f"/marketplace/access-requests/{req['id']}/revoke", json={"comment": "e2e"}).json()["state"] == "REVOKED"


# --------------------------------------------------------------------------- 7. quality incident → notifications
def _sink_events():
    return httpx.get(SINK_HOST, timeout=10).json() if SINK_HOST else []


def test_07_quality_incident_notifies_followers_and_routes(users):
    root, bob = users["root"], users["bob"]
    if SINK:
        routes = root.get("/notify/routes").json()
        routes = [r for r in routes if r["id"] != "e2e-sink"] + [
            {"id": "e2e-sink", "channel": "webhook", "target": SINK, "event_types": ["quality.incident.raised"]}]
        assert root.put("/notify/routes", json=routes).status_code == 200
    m = root.post("/quality/monitors", json={"name": f"am volume {RUN}", "dataset_urn": DS, "type": "volume",
                                              "config": {"source": "datahub", "min_rows": 5000}, "severity": "HIGH"})
    assert m.status_code == 201, m.text
    ev = eventually(lambda: (lambda e: e if e["status"] == "FAIL" else None)(
        root.post(f"/quality/monitors/{m.json()['id']}/run").json()), timeout=60)
    assert ev["status"] == "FAIL"
    eventually(lambda: [n for n in bob.get("/notify/inbox").json() if n["type"] == "quality.incident.raised"],
               timeout=30)
    if SINK_HOST:   # delivered by dhe-notifier within its 10 s cycle
        hit = eventually(lambda: [e for e in _sink_events() if e.get("type") == "quality.incident.raised"
                                  and e.get("entity_urn") == DS], timeout=60)
        assert hit[0]["url"].endswith(DS)


# --------------------------------------------------------------------------- 8. certification + inline actions
def test_08_certification_request_and_inline_approval(users):
    root, bob = users["root"], users["bob"]
    wf = bob.post("/governance/workflows", json={"kind": "CERTIFICATION", "entity_urn": DS_DOWN, "submit": True})
    assert wf.status_code == 201, wf.text
    wid = wf.json()["id"]
    assert wf.json()["state"] == "SUBMITTED"
    assert bob.post(f"/notify/actions/inline/workflow/approve/{wid}").status_code == 403   # requester can't approve
    r = root.post(f"/notify/actions/inline/workflow/approve/{wid}")
    assert r.status_code == 200, r.text
    done = root.get(f"/governance/workflows/{wid}").json()
    assert done["state"] == "APPROVED" and [a["actor"] for a in done["approvals"]] == [ROOT], done
    assert any(n["type"] == "certification.decided" for n in bob.get("/notify/inbox").json())


# --------------------------------------------------------------------------- 9. context agents
def test_09_context_miner_finds_drift_and_accepted_findings_write_native_metadata(users):
    root = users["root"]
    sqls = [
        f"select region, sum(amount) as revenue_{RUN} from am_{RUN}.public.customers group by region",
        f"select sum(amount) as revenue_{RUN} from am_{RUN}.public.customers where region = 'EMEA'",
        f"select sum(amount - 0) as revenue_{RUN} from am_{RUN}.public.customers",
        f"select c.region, count(*) from am_{RUN}.public.customers c "
        f"join am_{RUN}.analytics.customer_orders o on c.email = o.customer_email group by 1",
    ]
    for i, sql in enumerate(sqls):
        q = f"urn:li:query:am_{RUN}_{i}"
        emit(q, "queryProperties", {"statement": {"value": sql, "language": "SQL"}, "source": "MANUAL",
                                    "created": STAMP, "lastModified": STAMP})
        emit(q, "querySubjects", {"subjects": [{"entity": DS}] + ([{"entity": DS_DOWN}] if "join" in sql else [])})
    def mined():
        # The miner scrolls entities from the search index, which lags writes by a few seconds.
        compose("exec", "-T", "dhe-context-miner", "dhe", "context-miner", "--once")
        found = root.get("/context/findings", params={"entity_urn": DS, "limit": 200}).json()
        d = [f for f in found if f["type"] == "DEFINITION_DRIFT" and "revenue" in f["title"]]
        j = [f for f in found if f["type"] == "JOIN_RELATIONSHIP" and set(f["entities"]) == {DS, DS_DOWN}]
        return (d, j) if d and j else None

    drift, join = eventually(mined, timeout=120, interval=5)
    assert any(n["type"] == "context.drift" for n in users["carol"].get("/notify/inbox").json())   # owner told

    acc = root.post(f"/context/findings/{join[0]['id']}/accept")
    assert acc.status_code == 200 and acc.json()["applied_changes"] == 1, acc.text
    v = join[0]["proposed_changes"][0]["value"]
    rid = hashlib.sha1(f"{v['source']}|{v['source_field']}|{v['destination']}|{v['destination_field']}"
                       .encode()).hexdigest()[:24]
    rel = httpx.get(f"{GMS}/aspects/{urllib.parse.quote(f'urn:li:erModelRelationship:dhe-{rid}', safe='')}"
                    "?aspect=erModelRelationshipProperties&version=0", timeout=30,
                    headers={"Authorization": f"Bearer {TOKEN}", "X-RestLi-Protocol-Version": "2.0.0"})
    assert rel.status_code == 200, rel.text[:300]
    props = rel.json()["aspect"]["com.linkedin.ermodelrelation.ERModelRelationshipProperties"]
    assert {props["source"], props["destination"]} == {DS, DS_DOWN}
    # Dismissed findings stay dismissed across mining cycles.
    root.post(f"/context/findings/{drift[0]['id']}/dismiss")
    compose("exec", "-T", "dhe-context-miner", "dhe", "context-miner", "--once")
    still = [f for f in root.get("/context/findings", params={"status": "DISMISSED", "limit": 500}).json()
             if f["id"] == drift[0]["id"]]
    assert still
    onto = root.get("/context/ontology").json()
    assert onto["nodes"] and onto["edges"]


# --------------------------------------------------------------------------- 10. AI
def test_10_assistant_explainer_and_markdown_docs(users):
    root, bob = users["root"], users["bob"]
    chat = bob.post("/ai/chat", json={"message": f"customers am_{RUN}"})
    assert chat.status_code == 200, chat.text
    c = chat.json()
    assert c["conversation_id"] and c["reply_md"]
    again = bob.post("/ai/chat", json={"message": "and their owners?", "conversation_id": c["conversation_id"]})
    assert again.json()["conversation_id"] == c["conversation_id"]
    ex = root.post("/ai/explain/lineage", json={"urn": DS_DOWN}).json()
    assert ex["summary_md"] and any(x["urn"] == DS for x in ex["likely_causes"]), ex   # failing upstream monitor
    doc = root.post("/ai/docs/markdown", json={"urn": DS}).json()
    assert doc["markdown"].startswith("# customers") and "## Columns" in doc["markdown"]


# --------------------------------------------------------------------------- 11. embeds
def test_11_embed_tokens_are_scoped_origin_bound_and_revocable(users):
    bob = users["bob"]
    t = bob.post("/embed/v1/tokens", json={"urn": DS, "scopes": ["card", "quality"], "ttl_seconds": 3600})
    assert t.status_code == 201, t.text
    tok = t.json()["token"]
    assert "<dhe-" in t.json()["snippet_html"]
    url = f"{EDGE}/dhe/api/embed/v1/assets/{urllib.parse.quote(DS, safe='')}"
    ok = httpx.get(url, params={"token": tok}, headers={"Origin": "https://eu.tableau.example.com"}, timeout=30)
    assert ok.status_code == 200, ok.text
    assert ok.headers["access-control-allow-origin"] == "https://eu.tableau.example.com"
    assert "frame-ancestors" in ok.headers["content-security-policy"]
    assert ok.json()["urn"] == DS
    assert httpx.get(url, params={"token": tok}, headers={"Origin": "https://evil.example.net"}).status_code == 403
    lin = f"{EDGE}/dhe/api/embed/v1/lineage/{urllib.parse.quote(DS, safe='')}"
    assert httpx.get(lin, params={"token": tok}).status_code == 403                # scope not granted
    q = httpx.get(f"{EDGE}/dhe/api/embed/v1/quality/{urllib.parse.quote(DS, safe='')}", params={"token": tok})
    assert q.status_code == 200 and q.json()
    frame = httpx.get(f"{EDGE}/dhe/api/embed/v1/frame/card", params={"urn": DS, "token": tok})
    assert frame.status_code == 200 and "sdk.js" in frame.text
    jti = [x for x in bob.get("/embed/v1/tokens").json() if not x["revoked"]][0]["jti"]
    assert bob.delete(f"/embed/v1/tokens/{jti}").status_code == 204
    assert httpx.get(url, params={"token": tok}).status_code == 401


# --------------------------------------------------------------------------- 12. notifier maintenance
def test_12_notifier_sweeps_expired_grants():
    out = compose("exec", "-T", "dhe-notifier", "dhe", "notifier", "--once")
    assert "Traceback" not in out


# --------------------------------------------------------------------------- 13. consumer workspace in DataHub
def test_13_workspace_renders_inside_datahub_ui():
    """Log in to the DataHub UI and open the consumer workspace (MFE) with headless Chromium."""
    shots = os.environ.get("DHE_E2E_SCREENSHOT_DIR", "")
    script = os.path.join(os.path.dirname(__file__), "ui_workspace.js")
    out = subprocess.run(["node", script], capture_output=True, text=True, timeout=240, env={
        **os.environ, "EDGE": EDGE, "ASSET_URN": DS, "SHOTS": shots,
        "UI_USER": os.environ.get("DHE_E2E_UI_USER", "datahub"),
        "UI_PASSWORD": os.environ.get("DHE_E2E_UI_PASSWORD", "datahub")})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["discover"] and result["marketplace"] and result["asset"], result
    assert not result["errors"], result["errors"]
