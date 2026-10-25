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

from datahub_enterprise.app import create_api_app
from datahub_enterprise.collab.service import CollabService, ThreadIn, parse_mentions
from datahub_enterprise.context.miner import ContextStore, Snapshot, mine
from datahub_enterprise.context.sql import analyze_sql, same_definition
from datahub_enterprise.embed.service import EmbedError, EmbedService, TokenRequest, origin_allowed
from datahub_enterprise.gmsproxy.masking import build_plan, label_matches, mask_document, mask_scalar, mask_sql
from datahub_enterprise.gmsproxy.proxy import MaskingService, ProxyIdentity, create_proxy_app, verify_hs256
from datahub_enterprise.governance.abac import Policy, PolicyBundle
from datahub_enterprise.governance.attributes import AttributeResolver
from datahub_enterprise.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef
from datahub_enterprise.governance.personas import PersonaEngine, PersonaSpec, PurposeSpec
from datahub_enterprise.marketplace.service import AccessRequestIn, MarketplaceError, MarketplaceService, RequestPolicy
from datahub_enterprise.notify.hub import Action, Event, NotificationHub
from datahub_enterprise.notify.slack import SlackAuthError, verify_slack_signature
from datahub_enterprise.services import build_services

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

    app = create_proxy_app("http://gms", MaskingService(pdp, attrs), ProxyIdentity("sk", "http://gms"),
                           transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        bob = {"Authorization": f"Bearer {_jwt({'actorId': 'bob', 'exp': time.time() + 60}, b'sk')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset }"}, headers=bob)
        fp = r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"]
        assert fp[0]["sampleValues"] == ["j•••@•••.com"]
        assert r.headers["X-DHE-Masked"] and "purpose:" in r.headers["X-DHE-Masking-Policies"]

        carol = {"Authorization": f"Bearer {_jwt({'actorId': 'carol', 'exp': time.time() + 60}, b'sk')}"}
        r = c.post("/api/graphql", json={"query": "{ dataset }"}, headers=carol)
        assert "X-DHE-Masked" not in r.headers
        assert r.json()["data"]["dataset"]["datasetProfiles"][0]["fieldProfiles"][0]["sampleValues"] == [
            "jane@example.com"]
        assert c.get("/some/file", headers=bob).content == b"binary"


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
    assert item["action_url"].endswith(f"/dhe/asset/{DS}")
    assert hub.mark_read(CAROL, [item["id"]]) == 1 and hub.unread_count(CAROL) == 0

    token = hub.sign_action(Action("approve", "access_request", "ar_1", "Approve"), CAROL)
    assert hub.verify_action(token)["o"] == "ar_1"
    assert hub.consume_action_token(token, CAROL) == "done"
    with pytest.raises(PermissionError):
        hub.consume_action_token(token, CAROL)
    assert calls == [("approve", "ar_1", CAROL)]
    with pytest.raises(Exception):
        hub.verify_action(token[:-2] + "xx")


def test_notify_dispatch_uses_channel_senders(engine):
    hub = NotificationHub(engine, "https://dh.example.com", "s")
    sent = []

    class Fake:
        def send(self, row, _hub):
            sent.append(row["target"])
            return "fake"

    hub.channels["webhook"] = Fake()
    from datahub_enterprise.notify.hub import Route

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
    jti = svc.list_tokens(ALICE)[0]["jti"]
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
    alice, bob = {"X-DHE-Actor": ALICE}, {"X-DHE-Actor": BOB}
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
    from datahub_enterprise.governance.certification import CreateWorkflowRequest

    wf = s.workflows.create(CreateWorkflowRequest(kind="CERTIFICATION", entity_urn=DS), BOB)
    assert wf.state == "SUBMITTED"
    requested = [n for n in s.notify.inbox(CAROL) if n["type"] == "certification.requested"]
    assert requested, "owner should be asked to review"
    msg = s.notify.execute_action("workflow", "approve", wf.id, ALICE)
    assert wf.id in msg


def test_assistant_fallback_without_llm(api):
    _, s = api
    p = s.identity.resolve({"X-DHE-Actor": ALICE}, {})
    out = s.assistant.chat(p, "hello there")
    assert out["conversation_id"].startswith("conv_") and out["reply_md"]
    again = s.assistant.chat(p, "and again", out["conversation_id"])
    assert again["conversation_id"] == out["conversation_id"]
