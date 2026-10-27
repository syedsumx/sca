"""Live end-to-end tests for the Active Metadata layer (module E) against the compose stack.

Run after ``test_stack_smoke.py``'s prerequisites (``CAIRN_E2E=1``, ``CAIRN_E2E_TOKEN`` = root PAT). Also needs
``CAIRN_E2E_SLACK_SECRET`` (= ``CAIRN_SLACK_SIGNING_SECRET`` from ``.env``) for the Slack scenario and
``CAIRN_E2E_SINK`` (URL of an HTTP sink that records POSTs, reachable from cairn-notifier) for webhooks.

Each test acts as real Cairn Core users: ``bob`` (analyst, restricted) and ``carol`` (data owner / approver),
created with their own personal access tokens, so authorization is exercised end to end through
Cairn Core, the Cairn PDP, the masking proxy and cairn-api.
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

from test_stack_smoke import API, CORE, EDGE, TOKEN, emit, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

FRONTEND = os.environ.get("CAIRN_E2E_FRONTEND", "http://localhost:9002")
SLACK_SECRET = os.environ.get("CAIRN_E2E_SLACK_SECRET", "")
SINK = os.environ.get("CAIRN_E2E_SINK", "")            # as seen from inside the compose network
SINK_HOST = os.environ.get("CAIRN_E2E_SINK_HOST", "")  # same sink, as seen from the test runner
COMPOSE = ["docker", "compose", "-f", os.environ.get("CAIRN_E2E_COMPOSE", "docker-compose.cairn.yml")]

RUN = uuid.uuid4().hex[:8]
STARTED_MS = int(time.time() * 1000) - 60_000
ROOT = "urn:li:corpuser:admin"
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
    """GraphQL through cairn-web, i.e. frontend → cairn-core-proxy → Cairn Core (the masking path)."""
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
    for url in (f"{API}/health", f"{EDGE}/cairn/api/health", f"{EDGE}/cairn-ui/remoteEntry.js",
                f"{EDGE}/cairn-ui/embed/sdk.js", f"{EDGE}/cairn/api/embed/v1/sdk.js"):
        r = httpx.get(url, timeout=15, follow_redirects=True)
        assert r.status_code == 200, (url, r.status_code)
    assert "cairnConsumer" in httpx.get(f"{EDGE}/cairn-ui/remoteEntry.js", timeout=15).text
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
        return r if "purpose:" in r.headers.get("X-Cairn-Masking-Policies", "") and _samples(r).get("email") else None

    r = eventually(masked_by_purpose, timeout=90, interval=3)
    bob = _samples(r)
    assert bob["email"] == ["j•••@•••.com", "l•••@•••.org"], bob          # partial mask
    assert bob["region"] == ["EMEA", "APAC"]                                # unlabelled column untouched
    assert int(r.headers["X-Cairn-Masked"]) >= 1
    assert "mask-pii-for-non-stewards" in r.headers["X-Cairn-Masking-Policies"]   # base bundle rule, by id
    carol = _samples(frontend_gql(tokens["carol"], PROFILE_Q, {"urn": DS}))
    assert carol["email"] == ["jane.doe@example.com", "li@example.org"]  # purpose holder sees values
    # Direct Cairn Core access (service path) is not masked; only the frontend path goes through the proxy.
    direct = httpx.post(f"{CORE}/api/graphql", json={"query": PROFILE_Q, "variables": {"urn": DS}},
                        headers={"Authorization": f"Bearer {tokens['root']}"}, timeout=30)
    assert _samples(direct)["email"][0] == "jane.doe@example.com"


def _openapi_emails(body, urn=None) -> list:
    """Email sample lists under DS in an OpenAPI response (one aspect, an entity, or a list of entities)."""
    found = []
    if isinstance(body, dict):
        urn = body.get("urn", urn)
        if urn in (None, DS):
            found += [p.get("sampleValues") for p in body.get("fieldProfiles") or [] if p.get("fieldPath") == "email"]
        for v in body.values():
            found += _openapi_emails(v, urn)
    elif isinstance(body, list):
        for v in body:
            found += _openapi_emails(v, urn)
    return found


def test_03b_masking_covers_the_openapi_read_paths(users):
    tokens, ds = users["tokens"], urllib.parse.quote(DS, safe="")
    paths = [f"/openapi/v3/entity/dataset/{ds}/datasetprofile",                      # one aspect
             f"/openapi/v3/entity/dataset/{ds}?aspects=datasetProfile",               # one entity
             f"/openapi/v2/timeseries/dataset/datasetProfile?count=1000&startTimeMillis={STARTED_MS}",  # v2 scroll
             f"/openapi/v3/entity/dataset?aspects=datasetProfile&count=50&query=am_{RUN}"]  # entity list
    for path in paths:
        def masked(path=path):
            r = httpx.get(f"{FRONTEND}{path}", headers={"Authorization": f"Bearer {tokens['bob']}"}, timeout=60)
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            return r if _openapi_emails(r.json()) else None   # the list endpoint is search-backed
        r = eventually(masked, timeout=60, interval=3)
        assert _openapi_emails(r.json()) == [["j•••@•••.com", "l•••@•••.org"]], (path, r.text[:500])
        assert int(r.headers["X-Cairn-Masked"]) >= 1, path
        carol = httpx.get(f"{FRONTEND}{path}", headers={"Authorization": f"Bearer {tokens['carol']}"}, timeout=60)
        assert _openapi_emails(carol.json()) == [["jane.doe@example.com", "li@example.org"]], path


# --------------------------------------------------------------------------- 4. propagation
def _field_labels(urn: str, path: str) -> tuple[set, set]:
    r = httpx.get(f"{CORE}/aspects/{urllib.parse.quote(urn, safe='')}?aspect=editableSchemaMetadata&version=0",
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
               "actions": [{"action_id": "cairn:access_request:approve", "value": req["id"]}]}
    out = slack_post("/integrations/slack/interactions", {"payload": json.dumps(payload)})
    assert out.status_code == 200 and out.json()["text"].startswith("✅"), out.text
    assert bob.get(f"/marketplace/access-requests/{req['id']}").json()["state"] == "APPROVED"
    assert any(n["type"] == "access.granted" for n in bob.get("/notify/inbox").json())
    card = [c for c in bob.get("/marketplace/products", params={"q": RUN}).json() if c["urn"] == PRODUCT][0]
    assert card["access"]["status"] == "GRANTED"

    # Slash command search runs as the linked Cairn Core user.
    cmd = slack_post("/integrations/slack/commands", {"command": "/cairn", "text": "search customers",
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
                                              "config": {"source": "catalog", "min_rows": 5000}, "severity": "HIGH"})
    assert m.status_code == 201, m.text
    ev = eventually(lambda: (lambda e: e if e["status"] == "FAIL" else None)(
        root.post(f"/quality/monitors/{m.json()['id']}/run").json()), timeout=60)
    assert ev["status"] == "FAIL"
    eventually(lambda: [n for n in bob.get("/notify/inbox").json() if n["type"] == "quality.incident.raised"],
               timeout=30)
    if SINK_HOST:   # delivered by cairn-notifier within its 10 s cycle
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
        compose("exec", "-T", "cairn-context-miner", "cairn", "context-miner", "--once")
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
    rel = httpx.get(f"{CORE}/aspects/{urllib.parse.quote(f'urn:li:erModelRelationship:cairn-{rid}', safe='')}"
                    "?aspect=erModelRelationshipProperties&version=0", timeout=30,
                    headers={"Authorization": f"Bearer {TOKEN}", "X-RestLi-Protocol-Version": "2.0.0"})
    assert rel.status_code == 200, rel.text[:300]
    props = rel.json()["aspect"]["com.linkedin.ermodelrelation.ERModelRelationshipProperties"]
    assert {props["source"], props["destination"]} == {DS, DS_DOWN}
    # Dismissed findings stay dismissed across mining cycles.
    root.post(f"/context/findings/{drift[0]['id']}/dismiss")
    compose("exec", "-T", "cairn-context-miner", "cairn", "context-miner", "--once")
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
    assert "<cairn-" in t.json()["snippet_html"]
    url = f"{EDGE}/cairn/api/embed/v1/assets/{urllib.parse.quote(DS, safe='')}"
    ok = httpx.get(url, params={"token": tok}, headers={"Origin": "https://eu.tableau.example.com"}, timeout=30)
    assert ok.status_code == 200, ok.text
    assert ok.headers["access-control-allow-origin"] == "https://eu.tableau.example.com"
    assert "frame-ancestors" in ok.headers["content-security-policy"]
    assert ok.json()["urn"] == DS
    assert httpx.get(url, params={"token": tok}, headers={"Origin": "https://evil.example.net"}).status_code == 403
    lin = f"{EDGE}/cairn/api/embed/v1/lineage/{urllib.parse.quote(DS, safe='')}"
    assert httpx.get(lin, params={"token": tok}).status_code == 403                # scope not granted
    q = httpx.get(f"{EDGE}/cairn/api/embed/v1/quality/{urllib.parse.quote(DS, safe='')}", params={"token": tok})
    assert q.status_code == 200 and q.json()
    frame = httpx.get(f"{EDGE}/cairn/api/embed/v1/frame/card", params={"urn": DS, "token": tok})
    assert frame.status_code == 200 and "sdk.js" in frame.text
    jti = [x for x in bob.get("/embed/v1/tokens").json() if not x["revoked"]][0]["jti"]
    assert bob.delete(f"/embed/v1/tokens/{jti}").status_code == 204
    assert httpx.get(url, params={"token": tok}).status_code == 401


# --------------------------------------------------------------------------- 12. notifier maintenance
def test_12_notifier_sweeps_expired_grants():
    out = compose("exec", "-T", "cairn-notifier", "cairn", "notifier", "--once")
    assert "Traceback" not in out


# --------------------------------------------------------------------------- 13. consumer workspace in Cairn Core
def test_13_workspace_renders_inside_catalog_ui():
    """Log in to the Cairn Core UI and open the consumer workspace (MFE) with headless Chromium."""
    shots = os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", "")
    script = os.path.join(os.path.dirname(__file__), "ui_workspace.js")
    out = subprocess.run(["node", script], capture_output=True, text=True, timeout=240, env={
        **os.environ, "EDGE": EDGE, "ASSET_URN": DS, "SHOTS": shots,
        "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
        "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"]})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["discover"] and result["marketplace"] and result["asset"], result
    assert not result["errors"], result["errors"]


def test_13b_workspace_sections_are_catalog_navigation_entries():
    """One navigation: each workspace section is an entry in the catalog's left navigation, and
    switching sections inside the workspace moves the catalog's navigation with it."""
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_nav.js")], capture_output=True,
                         text=True, timeout=240, env={
                             **os.environ, "EDGE": EDGE, "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", ""),
                             "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
                             "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"]})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert not result["missing"] and not result["errors"], result
    assert result["viaShell"] == {"url": "/mfe/cairn-requests#/requests", "active": "#/requests"}, result
    assert result["viaWorkspace"] == {"url": "/mfe/cairn-marketplace#/marketplace", "active": "#/marketplace"}, result


# --------------------------------------------------------------------------- authorization mode
AUTHZ_MODE = os.environ.get("CAIRN_E2E_AUTHZ_MODE", "additive")
SERVICE_TOKEN = os.environ.get("CAIRN_E2E_PDP_TOKEN", "")
VIEW_Q = "query($u: String!) { dataset(urn: $u) { urn properties { name } } }"


def test_14_cairn_deny_is_enforced_only_in_authoritative_mode(users):
    """A Cairn deny on something the native policies allow (bob viewing DS).

    Authoritative mode: Cairn is the only authorizer, so Cairn Core refuses bob.
    Additive mode: the authorizer chain is first-ALLOW-wins, so the native allow still applies."""
    root = users["root"]
    saved = root.get("/governance/policies").json()["policies"]
    deny = {"id": f"e2e-deny-view-{RUN}", "description": "e2e: bob may not view this dataset", "effect": "deny",
            "priority": 500, "actions": ["VIEW_ENTITY_PAGE"],
            "condition": {"all": [{"attr": "subject.urn", "op": "eq", "value": BOB},
                                  {"attr": "resource.urn", "op": "eq", "value": DS}]}}
    assert root.put("/governance/policies", json={"policies": saved + [deny]}).status_code == 200
    try:
        def pdp_denies():
            r = httpx.post(f"{API}/governance/v1/authorize", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                           json={"actor": BOB, "privilege": "VIEW_ENTITY_PAGE",
                                 "resource": {"type": "dataset", "urn": DS}}, timeout=30)
            return r.status_code == 200 and r.json()["decision"] == "DENY"
        eventually(pdp_denies, timeout=60)

        def bob_can_view() -> bool:
            body = frontend_gql(users["tokens"]["bob"], VIEW_Q, {"u": DS}).json()
            # A hidden asset keeps only its identity (urn, type): its properties are gone.
            return bool(((body.get("data") or {}).get("dataset") or {}).get("properties")) and not body.get("errors")

        if AUTHZ_MODE == "authoritative":
            eventually(lambda: not bob_can_view(), timeout=120, interval=5)     # Cairn Core refuses bob
            assert frontend_gql(users["tokens"]["carol"], VIEW_Q, {"u": DS}).json()["data"]["dataset"]
        else:
            time.sleep(65)                                                       # past every decision cache
            assert bob_can_view(), "additive mode: the native allow must still apply"
    finally:
        root.put("/governance/policies", json={"policies": saved})
    if AUTHZ_MODE == "authoritative":
        eventually(bob_can_view, timeout=120, interval=5)                        # access returns after removal


def test_15_metadata_changes_reach_cached_decisions_within_seconds(users):
    """Tagging a dataset changes cached PDP decisions at once (MCL invalidation), not after the
    attribute TTL (60 s) or decision TTL (30 s)."""
    root = users["root"]
    tag = f"urn:li:tag:e2e-quarantine-{RUN}"
    saved = root.get("/governance/policies").json()["policies"]
    deny = {"id": f"e2e-quarantine-{RUN}", "description": "e2e: quarantined data is hidden", "effect": "deny",
            "priority": 500, "actions": ["VIEW_DATASET_PROFILE"],
            "condition": {"attr": "resource.tags", "op": "contains", "value": tag}}
    assert root.put("/governance/policies", json={"policies": saved + [deny]}).status_code == 200

    def decision():
        r = httpx.post(f"{API}/governance/v1/authorize", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                       json={"actor": BOB, "privilege": "VIEW_DATASET_PROFILE",
                             "resource": {"type": "dataset", "urn": DS}}, timeout=30)
        assert r.status_code == 200, r.text
        return r.json()["decision"]

    try:
        eventually(lambda: decision() != "DENY", timeout=60)       # bundle loaded, attributes now cached
        emit(DS, "globalTags", {"tags": [{"tag": tag}]})
        started = time.monotonic()
        eventually(lambda: decision() == "DENY", timeout=15, interval=0.5)
        took = time.monotonic() - started
        emit(DS, "globalTags", {"tags": []})
        eventually(lambda: decision() != "DENY", timeout=15, interval=0.5)
        assert took < 15
    finally:
        root.put("/governance/policies", json={"policies": saved})
        emit(DS, "globalTags", {"tags": []})


def test_16_auto_monitor_rules_create_run_alert_and_retire_monitors(users):
    """A rule file selects this run's dataset; the sweep creates its monitor, the monitor fails and
    alerts its webhook, and removing the rule retires the monitor."""
    root = users["root"]
    rule_file = os.path.join(os.path.dirname(__file__), "..", "..", "config", "quality", f"e2e-{RUN}.yaml")
    rule = {"rules": [{"id": f"e2e-{RUN}", "select": {"attr": "resource.urn", "op": "eq", "value": DS},
                       "monitors": [{"type": "volume", "severity": "HIGH", "config": {"min_rows": 10 ** 9},
                                     **({"notify": [SINK]} if SINK else {})}]}]}
    with open(rule_file, "w") as f:
        json.dump(rule, f)                        # JSON is YAML
    try:
        assert f"e2e-{RUN}" in [r["id"] for r in root.get("/quality/auto/rules").json()["rules"]]
        def preview():   # the dataset scan is search-backed: a just-seeded dataset shows up within seconds
            found = [m for m in root.post("/quality/auto/sweep?dry_run=true").json()["created"]
                     if m.startswith(f"auto-e2e-{RUN}")]
            return found or None
        mine = eventually(preview, timeout=60)
        assert len(mine) == 1, mine
        assert root.get(f"/quality/monitors/{mine[0]}").status_code == 404        # dry run changed nothing
        assert mine[0] in root.post("/quality/auto/sweep").json()["created"]
        monitor = root.get(f"/quality/monitors/{mine[0]}").json()
        assert monitor["dataset_urn"] == DS and monitor["created_by"] == "auto-monitors"
        ev = root.post(f"/quality/monitors/{mine[0]}/run").json()
        assert ev["status"] == "FAIL", ev
        if SINK_HOST:
            eventually(lambda: [e for e in _sink_events() if e.get("monitor_id") == mine[0]], timeout=30)
    finally:
        os.remove(rule_file)
    assert mine[0] in root.post("/quality/auto/sweep").json()["removed"]
    assert root.get(f"/quality/monitors/{mine[0]}").status_code == 404


def test_16b_shipped_rule_monitors_certified_datasets(users):
    """cairn-api creates the certification property at start; the default rule then puts freshness
    and volume monitors on every certified dataset."""
    cert = "urn:li:structuredProperty:io.cairn.certification.status"
    assert eventually(lambda: gql("""query($u: String!) { entity(urn: $u) { urn } }""", {"u": cert})["entity"],
                      timeout=120)
    emit(DS_DOWN, "structuredProperties", {"properties": [{"propertyUrn": cert, "values": [{"string": "CERTIFIED"}]}]})

    def planned():   # pending in a preview, or already created by the scheduler's own sweep
        root = users["root"]
        pending = [m for m in root.post("/quality/auto/sweep?dry_run=true").json()["created"]
                   if m.startswith("auto-certified-")]
        existing = [m["id"] for m in root.get("/quality/monitors", params={"dataset_urn": DS_DOWN}).json()
                    if m["id"].startswith("auto-certified-")]
        mine = pending + existing
        return mine if len(mine) >= 2 else None
    assert eventually(planned, timeout=60)


_FAKE_MODEL = r'''
import json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        props = req["format"].get("properties", {})
        if "unknowns" in props:
            out = {"summary": "s", "description": "d", "columns": [], "suggested_terms": [], "confidence": 0.5,
                   "unknowns": []}
        elif "citations" in props:
            out = {"answer": "a", "citations": []}
        elif "summary_md" in props:
            out = {"summary_md": "m"}
        else:
            out = {"intent": "find", "query": "*", "entity_types": [], "filters": [], "limit": 5,
                   "lineage": {"direction": "NONE", "target": "", "max_hops": 1}}
        body = json.dumps({"message": {"content": json.dumps(out)}, "prompt_eval_count": 120,
                           "eval_count": 30}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
env = dict(os.environ, CAIRN_LLM_PROVIDER="ollama", CAIRN_OLLAMA_URL=f"http://127.0.0.1:{srv.server_port}")
r = subprocess.run(["cairn", "ai", "eval", "--live", "--json", "/tmp/ai-eval.json"], env=env,
                   capture_output=True, text=True)
print(open("/tmp/ai-eval.json").read())
'''


def test_17_ai_usage_is_metered_and_evals_ship_in_the_image(users):
    """The shipped eval cases pass in the image; a live run meters tokens per call; usage reporting
    shows each user their own and everyone's only to operators."""
    out = subprocess.run(COMPOSE + ["exec", "-T", "cairn-api", "cairn", "ai", "eval"], capture_output=True,
                         text=True, timeout=300, cwd=os.path.join(os.path.dirname(__file__), "..", ".."))
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "12/12 AI eval cases passed" in out.stdout

    out = subprocess.run(COMPOSE + ["exec", "-T", "cairn-api", "python", "-"], input=_FAKE_MODEL, capture_output=True,
                         text=True, timeout=300, cwd=os.path.join(os.path.dirname(__file__), "..", ".."))
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout)
    assert report["mode"] == "live" and report["provider"] == "ollama"
    called = [r for r in report["results"] if r["model_calls"]]
    assert called and all(r["tokens"] == 150 * r["model_calls"] for r in called), report["results"]
    assert report["summary"]["cost_usd"] == 0.0                     # self-hosted models cost nothing

    bob, root = users["bob"], users["root"]
    assert bob.post("/ai/ask", json={"question": "customers with contact details"}).status_code == 200
    mine = bob.get("/ai/usage", params={"mine": "true"}).json()
    assert set(mine) >= {"groups", "total", "budget"} and mine["total"]["calls"] == 0   # no model configured
    assert bob.get("/ai/usage").status_code == 403
    via_edge = httpx.get(f"{EDGE}/cairn/api/ai/usage", params={"group_by": "day", "days": 7}, timeout=30,
                         headers={"Authorization": f"Bearer {users['tokens']['root']}"})
    assert via_edge.status_code == 200 and via_edge.json()["group_by"] == "day"
    assert root.get("/ai/usage", params={"group_by": "nope"}).status_code == 400


def test_18_semantic_search_similar_assets_and_index_upkeep(users):
    """New assets reach the semantic index as they are written; search explains its matches,
    similar assets follow shared columns, and the workspace shows both."""
    root, bob = users["root"], users["bob"]
    status = root.get("/ai/index/status").json()
    assert status["embedder"] == "hashing" and status["entries"] > 0, status
    assert bob.get("/ai/index/status").status_code == 403

    def found():
        hits = root.post("/ai/search/semantic", json={"query": f"am {RUN} customers contact details",
                                                      "limit": 10}).json()["results"]
        return next((h for h in hits if h["urn"] == DS), None)
    hit = eventually(found, timeout=90)
    assert {"customers", "contact"} <= set(hit["matched"]) and hit["platform"] == "postgres", hit
    assert not hit["description"].startswith("dataset ")
    # Keyword hits the page already shows can be left out.
    rest = root.post("/ai/search/semantic", json={"query": f"am {RUN} customers contact details",
                                                  "exclude": [DS]}).json()["results"]
    assert DS not in [h["urn"] for h in rest]

    # Similar assets: the downstream mart sharing its columns, and copies of the table from other runs
    # on a long-lived stack, rank first.
    similar = eventually(lambda: root.get("/ai/similar", params={"urn": DS}).json()["results"] or None, timeout=60)
    assert DS not in [h["urn"] for h in similar]
    assert {h["name"] for h in similar[:3]} <= {"customers", "customer_orders"}, similar

    # The workspace leaves out what keyword search already shows; pick a query where meaning adds something.
    def keyword(q):
        data = gql("""query($i: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $i) {
                        searchResults { entity { urn } } } }""", {"i": {"query": q, "start": 0, "count": 20}})
        return [r["entity"]["urn"] for r in data["searchAcrossEntities"]["searchResults"]]

    def adds(q):
        kw = keyword(q)
        extra = root.post("/ai/search/semantic", json={"query": q, "limit": 5, "exclude": kw,
                                                       "min_score": 0.15}).json()["results"]
        return (q, [h["urn"] for h in extra]) if extra else None
    query, expected = next(filter(None, map(adds, [f"custmer contactt detials {RUN}", "people we can email",
                                                     "buyer contact info", f"am {RUN} customers contact details"])))
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_semantic.js")], capture_output=True,
                         text=True, timeout=240, env={
                             **os.environ, "EDGE": EDGE, "QUERY": query,
                             "ASSET_URN": DS, "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", ""),
                             "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
                             "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"]})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["related"] == expected and not result["errors"], (result, expected)
    assert result["similar"] == [h["urn"] for h in similar], (result, similar)


# --------------------------------------------------------------------------- 19-20. Slack, Teams, embeds
def _double(kind: str) -> list:
    return httpx.get(f"{SINK_HOST.rstrip('/')}/{kind}", timeout=10).json()


def test_19_slack_and_teams_deliveries_are_valid_and_actionable(users):
    """With the Slack and Teams doubles (tests/e2e/sink.py) standing in for the real services: the
    notifier finds Carol in Slack by email, sends Block Kit Slack accepts and an Adaptive Card Teams
    renders; the card's signed link approves the request, and the Slack button then reports it is
    already decided through response_url."""
    from cairn.notify.channels import validate_adaptive_card, validate_blocks

    root, bob, carol = users["root"], users["bob"], users["carol"]
    if not SINK_HOST or not carol.get("/ui/config").json()["slack_enabled"]:
        pytest.skip("needs the e2e sink and CAIRN_SLACK_* pointing at it (see run.sh / CI)")
    pol = root.put(f"/marketplace/products/{urllib.parse.quote(PRODUCT, safe='')}/policy", json={
        "approvers": [CAROL], "max_duration_days": 30, "purposes": ["Churn analysis", "Other"],
        "requires_justification": True, "grant_permissions": ["view", "view_samples"]})
    assert pol.status_code == 200, pol.text
    settings = carol.get("/notify/settings").json()
    settings["channels"]["teams"] = {"enabled": True, "webhook_url": "https://169.254.169.254/latest/meta-data"}
    r = carol.put("/notify/settings", json=settings)
    assert r.status_code == 400 and "Microsoft webhook host" in r.text          # not a Teams webhook
    teams_hook = f"http://cairn-e2e-sink:8099/teams/carol-{RUN}"
    settings["channels"]["teams"] = {"enabled": True, "webhook_url": teams_hook}
    settings["channels"]["slack"] = {"enabled": True, "user_id": None}          # found by email
    assert carol.put("/notify/settings", json=settings).status_code == 200

    req = bob.post("/marketplace/access-requests", json={
        "product_urn": PRODUCT, "purpose": "Churn analysis", "duration_days": 7,
        "justification": "Validate Slack and Teams delivery"})
    assert req.status_code in (200, 201), req.text
    rid = req.json()["id"]

    def slack_message():
        for e in _double("slack"):
            blocks = (e.get("body") or {}).get("blocks") or []
            if e.get("method") == "chat.postMessage" and rid in json.dumps(blocks):
                return e
    msg = eventually(slack_message, timeout=90)
    assert validate_blocks(msg["body"]["blocks"]) == [], msg["body"]["blocks"]
    assert msg["auth"].startswith("Bearer xoxb")
    carol_email = f"{CAROL.split(':')[-1]}@example.com"
    lookups = [e for e in _double("slack") if e.get("method") == "users.lookupByEmail"]
    assert any(e["query"]["email"] == carol_email for e in lookups), lookups
    slack_user = msg["body"]["channel"]
    buttons = {b["action_id"]: b for b in msg["body"]["blocks"][1]["elements"]}
    assert buttons["cairn:access_request:approve"]["value"] == rid and "url" in buttons["cairn:open"]

    def teams_card():   # the hook was set just now, so its only access-request card is this one
        cards = [e for e in _double("teams") if e["path"] == f"/teams/carol-{RUN}"
                 and any(a.get("title") == "Approve" for a in
                         ((e["body"].get("attachments") or [{}])[0].get("content") or {}).get("actions") or [])]
        return cards[-1] if cards else None
    card = eventually(teams_card, timeout=90)
    assert validate_adaptive_card(card["body"]) == [], card["body"]
    links = {a["title"]: a["url"] for a in card["body"]["attachments"][0]["content"]["actions"]}
    approve = links["Approve"].replace("http://localhost:9000", EDGE)
    as_carol = {"Authorization": f"Bearer {users['tokens']['carol']}"}
    assert httpx.get(approve, headers={"Authorization": f"Bearer {users['tokens']['bob']}"}, timeout=30).status_code == 403
    page = httpx.get(approve, headers=as_carol, timeout=30)
    assert page.status_code == 200 and "<form" in page.text
    done = httpx.post(approve, headers=as_carol, data={"comment": "approved from Teams"}, timeout=30)
    assert done.status_code == 200, done.text
    assert bob.get(f"/marketplace/access-requests/{rid}").json()["state"] == "APPROVED"
    assert httpx.post(approve, headers=as_carol, timeout=30).status_code == 403          # single use

    # The Slack button for the same request: Carol is recognised from her Slack profile email,
    # and is told through response_url that it is already decided (the message is kept).
    payload = {"type": "block_actions", "user": {"id": slack_user},
               "response_url": f"http://cairn-e2e-sink:8099/slack/response/{RUN}",
               "actions": [{"action_id": "cairn:access_request:approve", "value": rid}]}
    out = slack_post("/integrations/slack/interactions", {"payload": json.dumps(payload)})
    assert out.status_code == 200 and out.json()["text"].startswith("⚠️"), out.text
    reply = eventually(lambda: next((e for e in _double("slack") if e.get("path") == f"/slack/response/{RUN}"),
                                    None), timeout=30)
    assert reply["body"]["replace_original"] is False and reply["body"]["response_type"] == "ephemeral"
    assert carol.post(f"/marketplace/access-requests/{rid}/revoke", json={"comment": "e2e"}).status_code == 200


def test_20_embeds_render_on_allowed_partner_pages_only(users):
    """A partner page on an allowed origin shows the embedded card (web component and iframe); the
    same page on an origin that is not allowed gets neither."""
    if not SINK_HOST:
        pytest.skip("needs the e2e sink (it serves the partner page)")
    bob = users["bob"]
    tok = bob.post("/embed/v1/tokens", json={"urn": DS, "scopes": ["card", "quality"], "ttl_seconds": 3600}).json()
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_embed.js")], capture_output=True,
                         text=True, timeout=240, env={
                             **os.environ, "EDGE": EDGE, "URN": DS, "TOKEN": tok["token"],
                             "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", "")})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    allowed, blocked = result["allowed"], result["blocked"]
    assert "customers" in allowed["component"] and "customers" in allowed["frame"], result
    assert "customers" not in blocked["component"] and "customers" not in blocked["frame"], result
    assert blocked["frame_blocked"], result
