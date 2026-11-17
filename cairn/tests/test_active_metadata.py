"""Active Metadata layer: personas & purposes, masking proxy, notifications, collaboration,
marketplace, context mining, embeds, assistant fallback and the HTTP wiring."""

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.collab.service import CollabService, ThreadIn, parse_mentions
from cairn.context.miner import ContextStore, Snapshot, mine
from cairn.context.sql import analyze_sql, same_definition
from cairn.embed.service import EmbedError, EmbedService, TokenRequest, origin_allowed
from cairn.coreproxy.masking import build_plan, label_matches, mask_document, mask_scalar, mask_sql
from cairn.coreproxy.proxy import MaskingService, ProxyIdentity, create_proxy_app, verify_hs256
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef
from cairn.governance.personas import PersonaEngine, PersonaSpec, PurposeSpec
from cairn.marketplace.service import AccessRequestIn, MarketplaceError, MarketplaceService, RequestPolicy
from cairn.notify.hub import Action, Event, NotificationHub
from cairn.notify.slack import SlackAuthError, verify_slack_signature
from cairn.quality.store import QualityStore
from cairn.services import build_services

from conftest import DS

ALICE, BOB, CAROL = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:carol"
PII_TERM = "urn:li:glossaryTerm:Classification.PII.Email"
FINANCE = "urn:li:domain:finance"
PRODUCT = "urn:li:dataProduct:orders"


def _dataset(graph, urn=DS, domain=FINANCE, product=PRODUCT):
    graph.aspects[(urn, "domains")] = {"domains": [domain]}
    graph.aspects[(urn, "ownership")] = {"owners": [{"owner": CAROL, "type": "DATAOWNER"}]}
    graph.aspects[(urn, "dataProducts")] = {"dataProducts": [{"destinationUrn": product}]}
    graph.aspects[(urn, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": PII_TERM}]}}]}
    graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": "email"}, {"fieldPath": "amount"}]}


@pytest.fixture
def gov(engine, graph):
    _dataset(graph)
    graph.aspects[(BOB, "groupMembership")] = {"groups": ["urn:li:corpGroup:analysts"]}
    attrs = AttributeResolver(graph)
    pdp = PolicyDecisionPoint(attrs, "authoritative", [], 0)
    personas = PersonaEngine(engine, pdp, None, attrs)
    return pdp, personas, attrs


def _decide(pdp, actor, privilege, urn=DS):
    return pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege,
                                       resource=ResourceRef(type=urn.split(":")[2], urn=urn)))


# --------------------------------------------------------------------------- personas & purposes
def test_persona_grants_scoped_access_and_purpose_denies_override(gov, graph):
    pdp, personas, _ = gov
    analysts = personas.upsert_persona(PersonaSpec(
        name="Finance analysts", members={"groups": ["urn:li:corpGroup:analysts"]},
        scope={"domains": [FINANCE]}, permissions=["view", "view_samples"]), ALICE)
    assert _decide(pdp, BOB, "VIEW_ENTITY_PAGE").decision == "ALLOW"
    assert _decide(pdp, BOB, "VIEW_DATASET_PROFILE").decision == "ALLOW"
    assert _decide(pdp, ALICE, "VIEW_ENTITY_PAGE").decision != "ALLOW"  # not a member
    other = "urn:li:dataset:(urn:li:dataPlatform:postgres,hr.people,PROD)"
    _dataset(graph, other, domain="urn:li:domain:hr")
    assert _decide(pdp, BOB, "VIEW_ENTITY_PAGE", other).decision != "ALLOW"  # out of scope

    # A purpose on PII: analysts are not allowed → strict deny on samples beats the persona grant,
    # and the masking obligation is attached to what remains visible.
    personas.upsert_purpose(PurposeSpec(name="Customer PII", tags=["PII"], allowed={"users": [CAROL]},
                                        deny=["view_samples"], mask={"strategy": "partial"}), ALICE)
    d = _decide(pdp, BOB, "VIEW_DATASET_PROFILE")
    assert d.decision == "DENY" and any(p.startswith("purpose:") for p in d.policy_ids)
    view = _decide(pdp, BOB, "VIEW_ENTITY_PAGE")
    assert view.decision == "ALLOW"
    assert personas.preview(analysts.id)["attribute_rules"] == 0


def test_time_boxed_grant_expires(gov):
    pdp, personas, _ = gov
    p = personas.upsert_persona(PersonaSpec(name="Temp", scope={"asset_urns": [DS]}), ALICE)
    now = int(time.time() * 1000)
    gid = personas.grant(p.id, ALICE, "manual", now + 60_000, CAROL)
    assert _decide(pdp, ALICE, "VIEW_ENTITY_PAGE").decision == "ALLOW"
    assert personas.expired_grants(now + 120_000)[0]["id"] == gid
    assert personas.revoke_grant(gid, CAROL)
    assert _decide(pdp, ALICE, "VIEW_ENTITY_PAGE").decision != "ALLOW"


def test_unknown_permission_bundle_rejected():
    with pytest.raises(ValueError):
        PersonaSpec(name="x", permissions=["teleport"])


# --------------------------------------------------------------------------- masking
def test_masking_primitives():
    assert mask_scalar("jane@example.com", "partial") == "j•••@•••.com"
    assert mask_scalar("secret", "redact") == "••••"
    assert mask_scalar("x", "null") is None
    assert mask_scalar("abc", "hash").startswith("sha256:")
    assert mask_sql("select * from t where email = 'a@b.c' and id = 42") == \
        "select * from t where email = ? and id = ?"
    assert label_matches("PII", [PII_TERM]) and not label_matches("PHI", [PII_TERM])
    assert label_matches("urn:li:tag:pii*", ["urn:li:tag:pii-high"])


def test_build_plan_strongest_strategy_wins():
    resource = {"field_labels": {"email": [PII_TERM]}, "tags": [], "glossary_terms": []}
    obs = [{"type": "mask_columns", "params": {"purpose": "a", "labels": ["PII"], "strategy": "partial"}},
           {"type": "mask_columns", "params": {"purpose": "b", "labels": ["PII.Email"], "strategy": "redact"}}]
    plan = build_plan(obs, resource)
    assert plan.fields == {"email"} and plan.strategy == "redact" and plan.policies == {"purpose:a", "purpose:b"}
    assert not build_plan([{"type": "mask_columns", "params": {"labels": ["PHI"]}}], resource).active


def _profile_doc():
    return {"data": {"dataset": {"urn": DS, "datasetProfiles": [{"fieldProfiles": [
        {"fieldPath": "email", "sampleValues": ["jane@example.com"], "min": "a@x.io"},
        {"fieldPath": "amount", "sampleValues": ["10", "20"]}]}],
        "usageStats": {"aggregations": {}}, "topSqlQueries": ["select 1 where email='jane@example.com'"]}}}


def test_mask_document_masks_only_labelled_fields():
    plan = build_plan([{"type": "mask_columns", "params": {"purpose": "pii", "labels": ["PII"],
                                                          "strategy": "partial"}}],
                      {"field_labels": {"email": [PII_TERM]}})
    doc = _profile_doc()
    res = mask_document(doc, lambda urn: plan)
    fp = doc["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"]
    assert fp[0]["sampleValues"] == ["j•••@•••.com"] and fp[1]["sampleValues"] == ["10", "20"]
    assert doc["data"]["dataset"]["topSqlQueries"] == ["select ? where email=?"]
    assert res.masked_values >= 3 and res.policies == {"purpose:pii"}


def _jwt(claims: dict, key: bytes) -> str:
    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    h, p = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()), b64(json.dumps(claims).encode())
    sig = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64(sig)}"


def test_verify_hs256():
    tok = _jwt({"actorId": "bob", "exp": time.time() + 60}, b"k")
    assert verify_hs256(tok, b"k")["actorId"] == "bob"
    assert verify_hs256(tok, b"other") is None
    assert verify_hs256(_jwt({"actorId": "bob", "exp": 1}, b"k"), b"k") is None


def test_proxy_masks_for_restricted_caller_and_streams_others(gov):
    pdp, personas, attrs = gov
    personas.upsert_persona(PersonaSpec(name="Analysts", members={"groups": ["urn:li:corpGroup:analysts"],
                                                                  "users": [CAROL]},
                                        permissions=["view", "view_samples"]), ALICE)
    personas.upsert_purpose(PurposeSpec(name="PII", tags=["PII"], allowed={"users": [CAROL]}, deny=[],
                                        mask={"strategy": "partial"}), ALICE)

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/graphql":
            return httpx.Response(200, json=_profile_doc())
        return httpx.Response(200, stream=httpx.ByteStream(b"binary"),
                              headers={"content-type": "application/octet-stream"})

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        bob = {"Authorization": f"Bearer {_jwt({'actorId': 'bob', 'exp': time.time() + 60}, b'sk')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset }"}, headers=bob)
        fp = r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"]
        assert fp[0]["sampleValues"] == ["j•••@•••.com"]
        assert r.headers["X-Cairn-Masked"] and "purpose:" in r.headers["X-Cairn-Masking-Policies"]

        carol = {"Authorization": f"Bearer {_jwt({'actorId': 'carol', 'exp': time.time() + 60}, b'sk')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset }"}, headers=carol)
        assert "X-Cairn-Masked" not in r.headers
        assert r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"][0]["sampleValues"] == [
            "jane@example.com"]
        assert c.get("/some/file", headers=bob).content == b"binary"


def test_proxy_reports_cairn_version(gov):
    pdp, _, attrs = gov
    from cairn import __version__

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"appConfig": {"appVersion": "v9.9.9", "authConfig": {}}}})

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        r = c.post("/api/v2/graphql", json={"query": "{ appConfig { appVersion } }"})
        assert r.json()["data"]["appConfig"] == {"appVersion": f"v{__version__}", "authConfig": {}}


def test_proxy_relabels_ingestion_run_logs(gov):
    pdp, _, attrs = gov
    report = ("Using existing bundled startup venv: /opt/datahub/venvs/datahub-gc-bundled\n"
              "  File \"/metadata-ingestion/src/datahub/ingestion/run/pipeline.py\"\n"
              "Successfully executed 'datahub ingest' (acryl-datahub 1.8.0) for DataHub")
    structured = '{"source": {"type": "datahub-gc"}, "sink": {"type": "datahub-rest"}, "events": 3}'
    doc = {"data": {"executionRequest": {"urn": "urn:li:dataHubExecutionRequest:x",
                                         "input": {"source": {"type": "SCHEDULED_INGESTION_SOURCE"}},
                                         "result": {"status": "SUCCESS", "report": report,
                                                    "structuredReport": {"serializedValue": structured}}}}}

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=doc)

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        out = c.post("/api/v2/graphql", json={"query": "{ executionRequest }"}).json()["data"]["executionRequest"]
    text = out["result"]["report"] + out["result"]["structuredReport"]["serializedValue"]
    assert "datahub" not in text.lower() and "acryl" not in text.lower()
    assert "cairn ingest" in text and "/opt/cairn/venvs/cairn-gc-bundled" in text
    assert json.loads(out["result"]["structuredReport"]["serializedValue"])["events"] == 3   # still valid JSON
    assert out["urn"] == "urn:li:dataHubExecutionRequest:x"                                  # identifiers untouched


def test_masking_fails_closed_when_pdp_errors(gov):
    pdp, _, attrs = gov

    def boom(*_a, **_k):
        raise RuntimeError("pdp down")

    pdp.decide = boom
    plan = MaskingService(pdp, attrs).plan(BOB, DS)
    assert plan.masks_field("email") and "fail-closed" in plan.policies


# --------------------------------------------------------------------------- notifications
def test_notify_publish_inbox_and_single_use_action_links(engine):
    hub = NotificationHub(engine, "https://dh.example.com", "link-secret", owners=lambda urn: [CAROL])
    calls = []
    hub.register_action("access_request", lambda a, o, actor, c: calls.append((a, o, actor)) or "done")
    ids = hub.publish(Event(type="access.requested", title="Access?", body="please", actor=BOB, entity_urn=DS,
                            include_owners=True, actions=[Action("approve", "access_request", "ar_1", "Approve")]))
    assert ids and hub.unread_count(CAROL) == 1 and hub.unread_count(BOB) == 0
    item = hub.inbox(CAROL)[0]
    assert item["action_url"].endswith(f"/cairn/asset/{DS}")
    assert hub.mark_read(CAROL, [item["id"]]) == 1 and hub.unread_count(CAROL) == 0

    token = hub.sign_action(Action("approve", "access_request", "ar_1", "Approve"), CAROL)
    assert hub.verify_action(token)["o"] == "ar_1"
    assert hub.consume_action_token(token, CAROL) == "done"
    with pytest.raises(PermissionError):
        hub.consume_action_token(token, CAROL)
    assert calls == [("approve", "ar_1", CAROL)]
    with pytest.raises(PermissionError):
        hub.verify_action(token[:-2] + "xx")


def test_notify_dispatch_uses_channel_senders(engine):
    hub = NotificationHub(engine, "https://dh.example.com", "s")
    sent = []

    class Fake:
        def send(self, row, _hub):
            sent.append(row["target"])
            return "fake"

    hub.channels["webhook"] = Fake()
    from cairn.notify.hub import Route

    hub.put_routes([Route(id="all", channel="webhook", target="https://hooks.example.com/x")])
    hub.publish(Event(type="context.drift", title="drift", body="b", actor=ALICE, entity_urn=DS))
    stats = hub.dispatch_pending()
    assert stats["sent"] == 1 and sent == ["https://hooks.example.com/x"]


def test_slack_signature():
    body, ts = b"payload=%7B%7D", str(int(time.time()))
    sig = "v0=" + hmac.new(b"sec", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    verify_slack_signature("sec", ts, body, sig)
    with pytest.raises(SlackAuthError):
        verify_slack_signature("sec", ts, body, "v0=bad")
    with pytest.raises(SlackAuthError):
        old = str(int(time.time()) - 3600)
        verify_slack_signature("sec", old, body,
                               "v0=" + hmac.new(b"sec", f"v0:{old}:".encode() + body, hashlib.sha256).hexdigest())


# --------------------------------------------------------------------------- collaboration
def test_threads_mentions_and_trending(engine):
    hub = NotificationHub(engine, "https://dh", "s")
    collab = CollabService(engine, hub)
    assert parse_mentions("ping @bob and @urn:li:corpuser:carol") == [BOB, CAROL]
    t = collab.create_thread(ALICE, ThreadIn(entity_urn=DS, title="Nulls?", body_md="@bob why nulls?"))
    collab.add_comment(BOB, t["id"], "Upstream bug, fixing.")
    assert any(n["type"] == "discussion.mention" for n in hub.inbox(BOB))
    assert any(n["type"] == "discussion.reply" for n in hub.inbox(ALICE))
    assert collab.set_status(ALICE, t["id"], "RESOLVED")["status"] == "RESOLVED"

    other = "urn:li:dataset:(urn:li:dataPlatform:postgres,x.y,PROD)"
    for actor in (ALICE, BOB, CAROL):
        collab.record_view(actor, DS)
    collab.record_view(ALICE, other)
    collab.bookmark(BOB, DS)
    top = collab.trending(5)
    assert top[0]["urn"] == DS and top[0]["score"] > top[1]["score"]
    collab.follow(CAROL, DS)
    assert collab.followers(DS) == [CAROL]


# --------------------------------------------------------------------------- marketplace
def _product_graph(graph):
    def handler(query, variables):
        product = {"urn": PRODUCT, "properties": {"name": "Orders", "description": "Orders product",
                                                  "numAssets": 1, "customProperties": []},
                   "ownership": {"owners": [{"owner": {"urn": CAROL}}]}, "entities": {"searchResults": []}}
        if "dataProduct(urn" in query:
            return {"dataProduct": product}
        return {"searchAcrossEntities": {"searchResults": [{"entity": product}]}}
    graph.graphql_handler = handler
    graph.aspects[(PRODUCT, "ownership")] = {"owners": [{"owner": CAROL}]}


def test_marketplace_human_approval_grants_persona_then_expires(engine, graph, gov):
    pdp, personas, attrs = gov
    _product_graph(graph)
    hub = NotificationHub(engine, "https://dh", "s", owners=lambda urn: [CAROL])
    mp = MarketplaceService(engine, graph, personas, hub, None, attrs)
    assert _decide(pdp, ALICE, "VIEW_DATASET_PROFILE").decision != "ALLOW"
    r = mp.create_request(ALICE, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                                 justification="Quarterly revenue analysis", duration_days=7))
    assert r["state"] == "PENDING" and r["approvers"] == [CAROL]
    assert any(n["type"] == "access.requested" for n in hub.inbox(CAROL))
    with pytest.raises(MarketplaceError):  # separation of duties
        mp.act(r["id"], "approve", ALICE)
    with pytest.raises(MarketplaceError):
        mp.create_request(ALICE, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                                 justification="again please", duration_days=7))
    out = mp.action_handler("approve", r["id"], CAROL, None)
    assert "approved" in out
    assert _decide(pdp, ALICE, "VIEW_DATASET_PROFILE").decision == "ALLOW"
    assert mp.list_products(ALICE)[0]["access"]["status"] == "GRANTED"

    stats = mp.sweep(int(time.time() * 1000) + 8 * 86_400_000)
    assert stats["expired"] == 1 and mp.get_request(r["id"])["state"] == "EXPIRED"


def test_marketplace_auto_approve_and_policy_validation(engine, graph, gov):
    _, personas, attrs = gov
    _product_graph(graph)
    mp = MarketplaceService(engine, graph, personas, None, None, attrs)
    with pytest.raises(MarketplaceError):
        mp.put_policy(PRODUCT, RequestPolicy(), BOB)  # not an owner
    mp.put_policy(PRODUCT, RequestPolicy(auto_approve={"enabled": True, "groups": ["urn:li:corpGroup:analysts"]},
                                         max_duration_days=30), CAROL)
    card = next(c for c in mp.list_products(BOB) if c["urn"] == PRODUCT)       # policies read in one batch
    assert card["request_policy"]["auto_approve"] is True and card["request_policy"]["max_duration_days"] == 30
    assert mp.get_policies([PRODUCT, "urn:li:dataProduct:none"])["urn:li:dataProduct:none"] == RequestPolicy()
    with pytest.raises(MarketplaceError):
        mp.create_request(BOB, AccessRequestIn(product_urn=PRODUCT, purpose="x", duration_days=90))
    r = mp.create_request(BOB, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                               justification="dashboards for finance", duration_days=14),
                          {"groups": ["urn:li:corpGroup:analysts"]})
    assert r["state"] == "APPROVED" and r["auto_approved"] is True and r["grant_id"]


# --------------------------------------------------------------------------- context mining
def test_sql_analysis_extracts_metrics_joins_and_drift():
    facts = analyze_sql("select c.region, sum(o.amount) as revenue from orders o "
                        "join customers c on o.customer_id = c.id group by 1", "postgres")
    assert not facts.error
    assert [m.name for m in facts.metrics] == ["revenue"]
    assert facts.joins and {facts.joins[0].left.column, facts.joins[0].right.column} == {"customer_id", "id"}
    assert same_definition("SUM(amount)", "sum( amount )")
    assert not same_definition("SUM(amount)", "SUM(amount - discount)")


def test_miner_detects_definition_drift_and_store_lifecycle(engine):
    q = [{"urn": f"urn:li:query:{i}", "sql": sql, "subjects": [DS], "platform": "postgres"} for i, sql in enumerate([
        "select sum(amount) as revenue from orders",
        "select sum(amount) as revenue from orders where 1=1",
        "select sum(amount - discount) as revenue from orders",
    ])]
    findings = mine(Snapshot(queries=q, datasets={DS: {}}))
    drift = [f for f in findings if f.type == "DEFINITION_DRIFT"]
    assert drift and drift[0].data["name"] == "revenue" and len(drift[0].data["variants"]) == 2

    hub = NotificationHub(engine, "https://dh", "s", owners=lambda urn: [CAROL])
    store = ContextStore(engine, None, CollabService(engine, hub), None, hub)
    assert store.upsert(findings)["new"] == len(findings)
    assert [n["type"] for n in hub.inbox(CAROL)] == ["context.drift"]
    assert store.upsert(findings)["new"] == 0  # fingerprints dedupe
    fid = store.list("DEFINITION_DRIFT")[0]["id"]
    store.set_status(fid, "DISMISSED", ALICE)
    store.upsert(findings)
    assert store.get(fid)["status"] == "DISMISSED"  # dismissed stays dismissed


# --------------------------------------------------------------------------- embeds
def test_embed_tokens_scopes_origins_and_revocation(engine):
    svc = EmbedService(engine, "embed-secret", ["https://*.tableau.example.com"], "https://dh")
    assert origin_allowed("https://eu.tableau.example.com", svc.allowed_origins)
    assert not origin_allowed("https://evil.example.com", svc.allowed_origins)
    minted = svc.mint(ALICE, TokenRequest(urn=DS, scopes=["card"]), DS)
    tok = minted["token"]
    assert svc.verify(tok, DS, "card", "https://eu.tableau.example.com")["sub"] == ALICE
    with pytest.raises(EmbedError):
        svc.verify(tok, DS, "lineage")
    with pytest.raises(EmbedError):
        svc.verify(tok, "urn:li:dataset:(urn:li:dataPlatform:x,other,PROD)", "card")
    with pytest.raises(EmbedError):
        svc.verify(tok, DS, "card", "https://evil.example.com")
    claims = svc.verify(tok, DS, "card")
    assert svc.card(claims, DS)["urn"] == DS
    q = EmbedService(engine, "embed-secret", [], "https://dh", quality_store=QualityStore(engine))
    qtok = q.mint(ALICE, TokenRequest(urn=DS, scopes=["quality"]), DS)["token"]
    assert q.quality(q.verify(qtok, DS, "quality"), DS)["status"] in ("UNKNOWN", "LEARNING", "HEALTHY", None)
    # The token minted first, by its own jti: list_tokens is newest first (by the second), and qtok came after it.
    jti = minted["jti"]
    assert jti in [t["jti"] for t in svc.list_tokens(ALICE)]
    assert svc.revoke(ALICE, jti)
    with pytest.raises(EmbedError):
        svc.verify(tok, DS, "card")


# --------------------------------------------------------------------------- HTTP wiring
@pytest.fixture
def api(settings, graph):
    settings.active.embed_signing_secret = "embed"
    _dataset(graph)
    s = build_services(settings, graph=graph, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="alice-all", effect="allow", actions=["*"],
        condition={"attr": "subject.urn", "op": "eq", "value": ALICE})]))
    return TestClient(create_api_app(s)), s


def test_api_wiring_personas_collab_notify_ui_config(api):
    c, s = api
    alice, bob = {"X-Cairn-Actor": ALICE}, {"X-Cairn-Actor": BOB}
    cfg = c.get("/ui/config", headers=alice).json()
    assert cfg["actor"] == ALICE and cfg["is_admin"] and cfg["features"]["embeds"]

    body = {"name": "Readers", "members": {"users": [BOB]}, "scope": {"domains": [FINANCE]}, "permissions": ["view"]}
    assert c.post("/governance/personas", json=body, headers=bob).status_code == 403
    r = c.post("/governance/personas", json=body, headers=alice)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert c.get(f"/governance/personas/{pid}", headers=alice).json()["compiled_policies"][0]["id"] == f"persona:{pid}"
    assert [p["id"] for p in c.get("/ui/config", headers=bob).json()["personas"]] == [pid]
    assert s.authorize(s.identity.resolve(bob, {}), "VIEW_ENTITY_PAGE", DS, raise_on_deny=False)

    assert c.put(f"/collab/bookmarks/{DS}", headers=bob).status_code in (200, 201)
    assert c.get("/collab/bookmarks", headers=bob).json()[0]["urn"] == DS
    t = c.post("/collab/threads", json={"entity_urn": DS, "title": "Q", "body_md": "@alice is this fresh?"},
               headers=bob)
    assert t.status_code == 201, t.text
    assert c.get("/notify/inbox/count", headers=alice).json()["unread"] >= 1
    assert c.get("/context/findings", headers=alice).status_code == 200


def test_workflow_events_notify_reviewers_with_inline_actions(api):
    _, s = api
    from cairn.governance.certification import CreateWorkflowRequest

    wf = s.workflows.create(CreateWorkflowRequest(kind="CERTIFICATION", entity_urn=DS), BOB)
    assert wf.state == "SUBMITTED"
    requested = [n for n in s.notify.inbox(CAROL) if n["type"] == "certification.requested"]
    assert requested, "owner should be asked to review"
    msg = s.notify.execute_action("workflow", "approve", wf.id, ALICE)
    assert wf.id in msg


def test_assistant_fallback_without_llm(api):
    _, s = api
    p = s.identity.resolve({"X-Cairn-Actor": ALICE}, {})
    out = s.assistant.chat(p, "hello there")
    assert out["conversation_id"].startswith("conv_") and out["reply_md"]
    again = s.assistant.chat(p, "and again", out["conversation_id"])
    assert again["conversation_id"] == out["conversation_id"]


def test_core_client_reads_token_file_lazily(tmp_path, settings):
    tok = tmp_path / "core_token"
    settings.core.token_file = str(tok)
    s = build_services(settings, with_ai=False)
    assert "Authorization" not in s.graph._headers  # bootstrap has not minted the token yet
    tok.write_text("tok-1\n")
    assert s.graph._headers["Authorization"] == "Bearer tok-1"


def test_proxy_hides_entities_the_caller_is_denied_in_authoritative_mode(gov):
    pdp, _, attrs = gov
    other = DS.replace("orders", "invoices")
    pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="hide-orders-from-bob", effect="deny", actions=["VIEW_ENTITY_PAGE"],
               condition={"all": [{"attr": "subject.urn", "op": "eq", "value": BOB},
                                  {"attr": "resource.urn", "op": "eq", "value": DS}]})]))
    doc = {"data": {"search": {"total": 2, "searchResults": [
        {"entity": {"urn": DS, "type": "DATASET", "__typename": "Dataset", "properties": {"name": "orders"},
                    "ownership": {"owners": [{"owner": {"urn": ALICE}}]}}},
        {"entity": {"urn": other, "type": "DATASET", "__typename": "Dataset", "properties": {"name": "invoices"}}},
    ]}, "tag": {"urn": "urn:li:tag:PII", "properties": {"name": "PII"}}}}

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=doc)

    masking = MaskingService(pdp, attrs)
    app = create_proxy_app("http://core", masking, ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    token = lambda who: {"Authorization": f"Bearer {_jwt({'actorId': who, 'exp': time.time() + 60}, b'sk')}"}  # noqa: E731
    with TestClient(app) as c:
        r = c.post("/api/v2/graphql", json={"query": "{ search }"}, headers=token("bob"))
        hits = r.json()["data"]["search"]["searchResults"]
        assert hits[0]["entity"] == {"urn": DS, "type": "DATASET", "__typename": "Dataset", "properties": None,
                                     "ownership": None}
        assert hits[1]["entity"]["properties"] == {"name": "invoices"}            # not denied
        assert r.json()["data"]["tag"]["properties"] == {"name": "PII"}           # vocabulary never hidden
        assert r.headers["X-Cairn-Restricted"] == "1"
        r = c.post("/api/v2/graphql", json={"query": "{ search }"}, headers=token("carol"))
        assert r.json()["data"]["search"]["searchResults"][0]["entity"]["properties"] == {"name": "orders"}
        assert "X-Cairn-Restricted" not in r.headers
        pdp.mode = "additive"                                                      # native allow wins
        r = c.post("/api/v2/graphql", json={"query": "{ search }"}, headers=token("bob"))
        assert r.json()["data"]["search"]["searchResults"][0]["entity"]["properties"] == {"name": "orders"}


def test_page_key_keeps_route_hashes_only():
    from cairn.embed.service import page_key

    assert page_key("https://acme.looker.com/dashboards/42?Region=EU#tile") == "https://acme.looker.com/dashboards/42"
    assert page_key("https://prod.online.tableau.com/#/site/acme/views/Sales/Overview?:iid=3") == \
        "https://prod.online.tableau.com/#/site/acme/views/Sales/Overview"
    assert page_key("https://app.powerbi.com/groups/me/reports/abc/ReportSection?experience=power-bi") == \
        "https://app.powerbi.com/groups/me/reports/abc/ReportSection"


def test_mark_all_read_reads_every_unread_one_of_that_person_only(engine):
    """Review of 7dca901: the inbox shows the newest 50, but Mark all read reads every unread notification (else the
    header's count never reaches zero), and never someone else's."""
    hub = NotificationHub(engine, "https://dh.example.com", "link-secret", owners=lambda urn: [CAROL])
    for i in range(60):
        hub.publish(Event(type="access.requested", title=f"n{i}", body="b", actor=BOB, recipients=[CAROL]))
    hub.publish(Event(type="access.requested", title="bob's", body="b", actor=CAROL, recipients=[BOB]))
    assert len(hub.inbox(CAROL)) == 50 and hub.unread_count(CAROL) == 60
    assert hub.mark_all_read(CAROL) == 60
    assert hub.unread_count(CAROL) == 0 and hub.unread_count(BOB) == 1
    assert hub.mark_all_read(CAROL) == 0


def test_embed_iframe_url_encodes_the_urn(engine):
    """A urn with '&', '#' or '+' in its name survives the iframe URL (it is percent-encoded, not spliced in)."""
    from urllib.parse import parse_qs, urlsplit

    urn = "urn:li:dataset:(urn:li:dataPlatform:x,sales&token=forged#a+b,PROD)"
    svc = EmbedService(engine, "embed-secret", [], "https://dh")
    minted = svc.mint(ALICE, TokenRequest(urn=urn, scopes=["card"]), urn)
    query = parse_qs(urlsplit(minted["iframe_url"]).query)
    assert query["urn"] == [urn] and query["token"] == [minted["token"]]


def test_discussion_notifications_skip_people_who_cannot_see_the_asset(engine):
    """A mention, a reply to a participant and a follower's notice go only to people who may see the asset: the
    comment and the asset's name never reach someone who cannot."""
    hub = NotificationHub(engine, "https://dh", "s", followers=lambda urn: [CAROL])
    sees = {ALICE, BOB}
    collab = CollabService(engine, hub, can_view_as=lambda person: (lambda urn: person in sees))
    t = collab.create_thread(ALICE, ThreadIn(entity_urn=DS, body_md="@bob and @carol: is ssn masked?"))
    assert any(n["type"] == "discussion.mention" for n in hub.inbox(BOB))
    assert hub.inbox(CAROL) == []
    sees.discard(BOB)                      # Bob loses access; he is still a participant of the thread
    collab.add_comment(ALICE, t["id"], "Still open.")
    assert [n["type"] for n in hub.inbox(BOB)] == ["discussion.mention"]
    assert hub.inbox(CAROL) == []


def test_resolving_and_reacting_need_the_right_to_comment(api):
    c, s = api
    alice, bob = {"X-Cairn-Actor": ALICE}, {"X-Cairn-Actor": BOB}
    t = c.post("/collab/threads", json={"entity_urn": DS, "body_md": "Nulls?"}, headers=alice).json()
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="alice-all", effect="allow", actions=["*"],
               condition={"attr": "subject.urn", "op": "eq", "value": ALICE}),
        Policy(id="bob-view", effect="allow", actions=["VIEW_ENTITY_PAGE"],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB}),
        Policy(id="bob-no-comment", effect="deny", actions=["CAIRN_COMMENT"],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    assert c.get(f"/collab/threads?entity_urn={DS}", headers=bob).status_code == 200
    assert c.post(f"/collab/threads/{t['id']}/resolve", headers=bob).status_code == 403
    cid = t["comments"][0]["id"]
    assert c.post(f"/collab/comments/{cid}/reactions", json={"emoji": "👍"}, headers=bob).status_code == 403
    assert c.post(f"/collab/threads/{t['id']}/resolve", headers=alice).json()["status"] == "RESOLVED"


def test_request_policy_and_preview_need_view_of_the_product(api):
    """Like the policy list and the request itself, one product's policy (its approvers and grants) and the preview of
    a request (auto-approval and approvers) are refused on a product the caller may not see."""
    c, _ = api
    alice, bob = {"X-Cairn-Actor": ALICE}, {"X-Cairn-Actor": BOB}
    req = {"product_urn": PRODUCT, "purpose": "Analytics", "justification": "quarterly reporting"}
    assert c.get(f"/marketplace/products/{PRODUCT}/policy", headers=bob).status_code == 403
    assert c.post("/marketplace/access-requests/preview", json=req, headers=bob).status_code == 403
    assert c.get(f"/marketplace/products/{PRODUCT}/policy", headers=alice).status_code == 200
    assert c.post("/marketplace/access-requests/preview", json=req, headers=alice).status_code == 200


def test_deleting_a_persona_ends_its_grants_so_a_new_one_with_its_id_does_not_inherit_them(engine):
    personas = PersonaEngine(engine)
    spec = PersonaSpec(name="Finance readers", scope={"domains": [FINANCE]}, permissions=["view"])
    pid = personas.upsert_persona(spec, ALICE).id
    personas.grant(pid, BOB, "manual", None, ALICE)
    assert pid in personas.memberships({"urn": BOB}, int(time.time() * 1000))
    assert personas.delete_persona(pid, ALICE)
    again = personas.upsert_persona(PersonaSpec(name="Finance readers", permissions=["view", "data_access"]), ALICE)
    assert again.id == pid
    assert pid not in personas.memberships({"urn": BOB}, int(time.time() * 1000))
    assert personas.list_grants(pid) == []
    personas.reload()                                     # the stored grants agree, not only the cache
    assert pid not in personas.memberships({"urn": BOB}, int(time.time() * 1000))


def test_proxy_fails_closed_for_a_bearer_token_it_cannot_identify(gov):
    """Cairn Core answered (it accepted the token), but the proxy's key does not verify it: values are masked."""
    pdp, _, attrs = gov

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_profile_doc())

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        other_key = {"Authorization": f"Bearer {_jwt({'actorId': 'bob', 'exp': time.time() + 60}, b'rotated')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset }"}, headers=other_key)
        assert r.status_code == 200 and "jane@example.com" not in r.text and '"10"' not in r.text
        assert r.headers.get("X-Cairn-Restricted") or "fail-closed" in r.headers["X-Cairn-Masking-Policies"]
        # No bearer token at all (Core's own service calls): passed through as before.
        r = c.post("/api/graphql", json={"query": "{ dataset }"})
        assert r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"][0]["sampleValues"] == [
            "jane@example.com"]


def test_proxy_refuses_graphql_aliases_on_governed_fields(gov):
    """Aliases rename the response keys the masking and view walkers look for (fieldProfiles, urn, statement):
    the proxy refuses them, in fragments too, and refuses documents it cannot parse."""
    pdp, personas, attrs = gov
    personas.upsert_persona(PersonaSpec(name="Analysts", members={"groups": ["urn:li:corpGroup:analysts"]},
                                        permissions=["view", "view_samples"]), ALICE)
    personas.upsert_purpose(PurposeSpec(name="PII", tags=["PII"], allowed={"users": [CAROL]}, deny=[],
                                        mask={"strategy": "partial"}), ALICE)
    profiles = [{"fieldPath": "email", "sampleValues": ["jane@example.com"], "min": "aa@example.com"}]
    shapes = {   # what Cairn Core answers for each aliased query
        "fp": {"data": {"dataset": {"urn": DS, "datasetProfiles": [{"fp": profiles}]}}},
        "id": {"data": {"dataset": {"id": DS, "datasetProfiles": [{"fieldProfiles": profiles}]}}},
        "u": {"data": {"dataset": {"u": DS, "properties": {"name": "orders", "description": "jane@example.com"}}}},
    }
    sent = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        query = json.loads(request.content)["query"]
        for alias, doc in shapes.items():
            if f"{alias}:" in query:
                return httpx.Response(200, json=doc)
        return httpx.Response(200, json=_profile_doc())

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    token = lambda who: {"Authorization": f"Bearer {_jwt({'actorId': who, 'exp': time.time() + 60}, b'sk')}"}  # noqa: E731
    refused = [
        '{ dataset(urn: "x") { urn datasetProfiles { fp: fieldProfiles { fieldPath sampleValues min } } } }',
        '{ dataset(urn: "x") { id: urn datasetProfiles { fieldProfiles { fieldPath sampleValues min } } } }',
        '{ dataset(urn: "x") { u: urn properties { name description } } }',
        'query Q { dataset(urn: "x") { ...F } } fragment F on Dataset { urn datasetProfiles { '
        'fieldProfiles { fieldPath s: sampleValues } } }',
        '{ dataset(urn: "x") { ... on Dataset { urn statement: description } } }',
        '{ entity(urn: "q") { ... on QueryEntity { properties { statement { v: value } } } } }',
        '{ dataset(urn: "x") { urn',                                   # does not parse
    ]
    with TestClient(app) as c:
        for who in ("bob", "alice"):
            for query in refused:
                r = c.post("/api/graphql", json={"query": query}, headers=token(who))
                assert r.status_code == 400 and "jane@example.com" not in r.text, (who, query)
            r = c.post("/api/graphql", json=[{"query": "{ me }"}, {"query": refused[0]}], headers=token(who))
            assert r.status_code == 400                                 # every document of a batch is checked
            r = c.post("/api/graphql", json={"extensions": {"persistedQuery": {"sha256Hash": "ab"}}},
                       headers=token(who))
            assert r.status_code == 400                                 # nothing to inspect: refused
            r = c.get("/api/v2/graphql", params={"query": refused[2]}, headers=token(who))
            assert r.status_code == 400
        assert not sent                                                 # none of them reached Cairn Core
        # Unaliased (or self-aliased) queries still pass and are masked as before.
        r = c.post("/api/graphql", json={"query": '{ dataset(urn: "x") { urn: urn datasetProfiles { '
                                                  'fieldProfiles { fieldPath sampleValues min } } } }'},
                   headers=token("bob"))
        assert r.status_code == 200
        assert r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"][0]["sampleValues"] == [
            "j•••@•••.com"]
        # The system actor is never masked, so its documents are not checked.
        system = {"Authorization": f"Bearer {_jwt({'actorId': '__datahub_system', 'exp': time.time() + 60}, b'sk')}"}
        assert c.post("/api/graphql", json={"query": refused[0]}, headers=system).status_code == 200


def test_proxy_restricts_entities_whose_urn_the_query_did_not_select(gov):
    """View restriction finds governed entities by their urn: the proxy adds it to every governed selection set
    (lookups by urn, entity fields, fragments on governed types) and strips it from the answer again."""
    pdp, _, attrs = gov
    pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="hide-orders-from-bob", effect="deny", actions=["VIEW_ENTITY_PAGE"],
               condition={"all": [{"attr": "subject.urn", "op": "eq", "value": BOB},
                                  {"attr": "resource.urn", "op": "eq", "value": DS}]})]))
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        seen.append(query)
        ds = {"properties": {"name": "orders", "description": "secret"}}
        hit = {"entity": {"__typename": "Dataset", "properties": {"name": "orders"}}}
        if "_cairnUrn" in query:                         # what Core answers for the injected document
            ds["_cairnUrn"] = DS
            hit["entity"]["_cairnUrn"] = DS
        return httpx.Response(200, json={"data": {"dataset": ds, "search": {"searchResults": [hit]}}})

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    token = lambda who: {"Authorization": f"Bearer {_jwt({'actorId': who, 'exp': time.time() + 60}, b'sk')}"}  # noqa: E731
    query = (f'{{ dataset(urn: "{DS}") {{ properties {{ name description }} }} '
             'search(input: {query: "*"}) { searchResults { entity { ... on Dataset { properties { name } } } } } }')
    with TestClient(app) as c:
        r = c.post("/api/graphql", json={"query": query}, headers=token("bob"))
        data = r.json()["data"]
        assert data["dataset"] == {"properties": None}                 # restricted, and no urn it did not ask for
        assert data["search"]["searchResults"][0]["entity"] == {"__typename": "Dataset", "properties": None}
        assert r.headers["X-Cairn-Restricted"] == "1" and "secret" not in r.text and "_cairnUrn" not in r.text
        r = c.post("/api/graphql", json={"query": query}, headers=token("carol"))
        assert r.json()["data"]["dataset"] == {"properties": {"name": "orders", "description": "secret"}}
        assert "_cairnUrn" not in r.text and "X-Cairn-Restricted" not in r.headers
        system = {"Authorization": f"Bearer {_jwt({'actorId': '__datahub_system', 'exp': time.time() + 60}, b'sk')}"}
        c.post("/api/graphql", json={"query": query}, headers=system)
        assert "_cairnUrn" not in seen[-1]                             # service actors' documents are not touched


def test_masking_fails_closed_without_a_dataset_in_context(gov):
    """Profile values or SQL text whose dataset urn the query did not select cannot be matched to a plan: they are
    masked for a caller who is not a service actor, instead of passing in clear."""
    pdp, personas, attrs = gov
    personas.upsert_persona(PersonaSpec(name="Analysts", members={"groups": ["urn:li:corpGroup:analysts"]},
                                        permissions=["view", "view_samples"]), ALICE)
    personas.upsert_purpose(PurposeSpec(name="PII", tags=["PII"], allowed={"users": [CAROL]}, deny=[],
                                        mask={"strategy": "partial"}), ALICE)
    doc = {"data": {"dataset": {"datasetProfiles": [{"fieldProfiles": [
        {"fieldPath": "email", "sampleValues": ["jane@example.com"], "min": "aa@example.com"}]}]},
        "entity": {"urn": "urn:li:query:q1", "properties": {"statement": {"value": "select 1 where e='jane'"}}}}}

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=doc)

    app = create_proxy_app("http://core", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://core"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        bob = {"Authorization": f"Bearer {_jwt({'actorId': 'bob', 'exp': time.time() + 60}, b'sk')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset { datasetProfiles { fieldProfiles { fieldPath "
                                                  "sampleValues min } } } }"}, headers=bob)
        assert r.status_code == 200 and "jane" not in r.text and "aa@example.com" not in r.text
        assert "fail-closed" in r.headers["X-Cairn-Masking-Policies"]
        # A REST call names its dataset in the path: that dataset's plan applies, not the fail-closed one.
        r = c.get(f"/aspects/{DS}", params={"aspect": "datasetProfile"}, headers=bob)
        assert r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"][0]["sampleValues"] == [
            "j•••@•••.com"]
        assert "fail-closed" not in r.headers["X-Cairn-Masking-Policies"]
    # A query entity whose subjects are selected is decided by its datasets (unmasked here), not failed closed.
    query_doc = {"urn": "urn:li:query:q2", "subjects": [{"dataset": {"urn": DS.replace("orders", "plain")}}],
                 "properties": {"statement": {"value": "select 1"}}}
    masked = build_plan([{"type": "mask_columns", "params": {"labels": ["x"]}}], {"tags": ["x"]})
    res = mask_document(query_doc, lambda urn: build_plan([], {}) if urn else masked)
    assert query_doc["properties"]["statement"]["value"] == "select 1" and res.masked_values == 0


def test_propagation_target_platforms_ignore_case():
    from cairn.governance.propagation_rules import PropagationRule

    rule = PropagationRule(id="pii", labels=["urn:li:tag:pii"], target_platforms=["Snowflake"])
    assert rule.targets_platform("urn:li:dataset:(urn:li:dataPlatform:snowflake,db.t,PROD)")
    assert not rule.targets_platform("urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)")


def test_deciding_findings_needs_edit_rights_and_ontology_proposals_manage_glossaries(api):
    """Dismissing a finding needs the edit rights accepting does; an ontology proposal decided through the context
    inbox needs MANAGE_GLOSSARIES, as on /v1/ontology."""
    from cairn.context.miner import Finding

    c, s = api
    bob = {"X-Cairn-Actor": BOB}
    term = {"kind": "create_term", "value": {"name": "Net revenue", "definition": "Revenue less refunds."}}
    s.context.upsert([
        Finding("METRIC_DEFINITION", ("mined", "revenue"), "Revenue", "sum(amount)", 0.9, [DS]),
        Finding("METRIC_DEFINITION", ("onto", "net"), "Net revenue", "a term", 0.9, [DS],
                proposed_changes=[term], data={"source": "ontology", "kind": "term"})])
    mined, onto = (next(f["id"] for f in s.context.list() if f["title"] == t) for t in ("Revenue", "Net revenue"))
    view_only = [Policy(id="bob-view", effect="allow", actions=["VIEW_ENTITY_PAGE"],
                        condition={"attr": "subject.urn", "op": "eq", "value": BOB})]
    alice_all = Policy(id="alice-all", effect="allow", actions=["*"],
                       condition={"attr": "subject.urn", "op": "eq", "value": ALICE})
    s.pdp.set_bundle(PolicyBundle(policies=[alice_all, *view_only]))
    assert c.post(f"/context/findings/{mined}/dismiss", headers=bob).status_code == 403
    s.pdp.set_bundle(PolicyBundle(policies=[alice_all, *view_only, Policy(
        id="bob-edit", effect="allow", actions=["EDIT_ENTITY_GLOSSARY_TERMS"],
        condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    assert c.post(f"/context/findings/{onto}/accept", headers=bob).status_code == 403
    assert c.post(f"/context/findings/{onto}/dismiss", headers=bob).status_code == 403
    assert s.context.get(onto)["status"] == "OPEN"
    assert c.post(f"/context/findings/{mined}/dismiss", headers=bob).json()["status"] == "DISMISSED"
    assert c.post(f"/context/findings/{onto}/dismiss", headers={"X-Cairn-Actor": ALICE}).status_code == 200


def test_assistant_explains_lineage_only_of_assets_the_caller_may_see(api):
    """The explain_lineage tool and the offline 'why' answer refuse an asset the caller may not see (its monitors and
    incidents are not theirs to read), as POST /ai/explain/lineage does."""
    _, s = api
    bob = s.identity.resolve({"X-Cairn-Actor": BOB}, {})
    alice = s.identity.resolve({"X-Cairn-Actor": ALICE}, {})
    with pytest.raises(PermissionError):
        s.assistant._run_tool(bob, "explain_lineage", {"urn": DS})
    assert s.assistant._run_tool(alice, "explain_lineage", {"urn": DS})[0]["urn"] == DS
    out = s.assistant.chat(bob, f"why is {DS} broken?")
    assert DS not in out["citations"] and not any(c["name"] == "explain_lineage" for c in out["tool_calls"])
    assert s.assistant.chat(alice, f"why is {DS} broken?")["citations"][0] == DS
