"""Negative protocol tests for the agent gateway: every attack in the threat model
(docs/site/agent-security.md) is attempted against the real HTTP surface and must fail closed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cairn.agent import crypto
from cairn.agent.api import _client_ip
from cairn.agent.client import AgentState, GatewayClient, GatewayHTTPError
from cairn.agent.gateway import EnqueueRequest, GatewayService, PoolSpec
from cairn.app import create_gateway_app
from cairn.common.proposals import make_mcp
from cairn.services import build_services

from conftest import DS

ADMIN = "urn:li:corpuser:admin"
PG_DB = "urn:li:container:pg-finance-db"
SNOWFLAKE_DB = "urn:li:container:snowflake-analytics"


@pytest.fixture
def gw(settings, graph, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_GATEWAY_MAX_BODY_BYTES", str(256 << 10))
    services = build_services(settings, graph=graph, with_ai=False)
    gateway = services.gateway
    gateway.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=4), ADMIN)
    graph.aspects[(SNOWFLAKE_DB, "dataPlatformInstance")] = {"platform": "urn:li:dataPlatform:snowflake"}
    http = TestClient(create_gateway_app(services))
    return services, gateway, http, tmp_path


def _agent(gateway, http, tmp_path, name):
    token = gateway.create_enrollment_token("finance", ADMIN)["token"]
    client = GatewayClient("http://testserver", AgentState(str(tmp_path / name)), http=http)
    client.enroll(token, hostname=name)
    return client


def _run(gateway, client):
    gateway.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"type": "postgres", "config": {}}}), ADMIN)
    return client.poll()["run_id"]


def _signed_post(http, client, path, body: bytes, sign_path=None, sign_body=None):
    headers = crypto.sign_request(client.state.identity, client.state.agent_id, "POST", sign_path or path,
                                  body if sign_body is None else sign_body)
    return http.post(path, content=body, headers=headers)


def _batch_body(gateway, client, run_id, seq, mcps, aad_run=None, aad_seq=None):
    env = crypto.seal(gateway.keys.identity.encryption_public, json.dumps(mcps).encode(),
                      crypto.aad_for(client.state.agent_id, aad_run or run_id, str(seq if aad_seq is None else aad_seq)))
    return json.dumps({"seq": seq, "envelope": env}).encode()


# --------------------------------------------------------------------------- enrolment
def test_enrollment_token_is_single_use_and_expires(gw):
    _, gateway, http, tmp = gw
    token = gateway.create_enrollment_token("finance", ADMIN)["token"]
    GatewayClient("http://testserver", AgentState(str(tmp / "a")), http=http).enroll(token, hostname="a")
    with pytest.raises(GatewayHTTPError) as e:
        GatewayClient("http://testserver", AgentState(str(tmp / "b")), http=http).enroll(token, hostname="b")
    assert e.value.status == 401
    expired = gateway.create_enrollment_token("finance", ADMIN, ttl_seconds=-1)["token"]
    with pytest.raises(GatewayHTTPError) as e:
        GatewayClient("http://testserver", AgentState(str(tmp / "c")), http=http).enroll(expired, hostname="c")
    assert e.value.status == 401


def test_enrollment_rejects_malformed_keys(gw):
    _, gateway, http, _ = gw
    token = gateway.create_enrollment_token("finance", ADMIN)["token"]
    r = http.post("/agents/enroll", json={"token": token, "signing_public": crypto.b64e(b"short"),
                                          "encryption_public": crypto.b64e(b"x" * 32), "hostname": "h", "version": "1"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- request signatures
def test_signature_binds_path_body_and_agent(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    b = _agent(gateway, http, tmp, "b")
    hb = f"/agents/{a.state.agent_id}/heartbeat"
    ok = b'{"running_task_ids":[]}'
    assert _signed_post(http, a, hb, ok).status_code == 200
    # Signed for another endpoint, or for another body.
    assert _signed_post(http, a, hb, ok, sign_path=f"/agents/{a.state.agent_id}/tasks/poll").status_code == 401
    assert _signed_post(http, a, hb, b'{"running_task_ids":["t"]}', sign_body=ok).status_code == 401
    # Agent b signs correctly as itself but calls agent a's endpoint.
    assert _signed_post(http, b, hb, ok).status_code == 403
    # No signature at all.
    assert http.post(hb, content=ok).status_code == 401
    # Bad timestamp format.
    headers = crypto.sign_request(a.state.identity, a.state.agent_id, "POST", hb, ok)
    headers[crypto.HEADER_TS] = "yesterday"
    assert http.post(hb, content=ok, headers=headers).status_code == 401


# --------------------------------------------------------------------------- batches
def test_batch_envelope_is_bound_to_agent_run_and_sequence(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    b = _agent(gateway, http, tmp, "b")
    run_a, run_b = _run(gateway, a), _run(gateway, b)
    path = f"/agents/{a.state.agent_id}/runs/{run_a}/batches"
    mcp = [make_mcp(DS, "status", {"removed": False})]
    # Envelope sealed for seq 0 but sent as seq 1 (reordering/replay).
    assert _signed_post(http, a, path, _batch_body(gateway, a, run_a, 1, mcp, aad_seq=0)).status_code == 400
    # Envelope sealed for another run.
    assert _signed_post(http, a, path, _batch_body(gateway, a, run_a, 0, mcp, aad_run=run_b)).status_code == 400
    # Tampered ciphertext.
    body = json.loads(_batch_body(gateway, a, run_a, 0, mcp))
    ct = bytearray(crypto.b64d(body["envelope"]["ct"]))
    ct[0] ^= 1
    body["envelope"]["ct"] = crypto.b64e(bytes(ct))
    assert _signed_post(http, a, path, json.dumps(body).encode()).status_code == 400
    # Writing into another agent's run.
    other = f"/agents/{a.state.agent_id}/runs/{run_b}/batches"
    assert _signed_post(http, a, other, _batch_body(gateway, a, run_b, 0, mcp)).status_code == 404
    # The genuine batch works once; the same sequence number again is refused.
    assert _signed_post(http, a, path, _batch_body(gateway, a, run_a, 0, mcp)).status_code == 200
    assert _signed_post(http, a, path, _batch_body(gateway, a, run_a, 0, mcp)).status_code == 409


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"seq": "1", "envelope": {}}', b'{"seq": 1, "envelope": []}',
                                     b'{"seq": true, "envelope": {}}', b"[]"])
def test_malformed_batch_payloads_are_400_not_500(gw, payload):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    run = _run(gateway, a)
    for kind in ("batches", "logs"):
        r = _signed_post(http, a, f"/agents/{a.state.agent_id}/runs/{run}/{kind}", payload)
        assert r.status_code == 400, (kind, payload, r.status_code, r.text)
    r = _signed_post(http, a, f"/agents/{a.state.agent_id}/runs/{run}/complete", payload)
    assert r.status_code == 400


def test_oversized_bodies_are_refused_before_reading(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    big = b"x" * (300 << 10)
    r = _signed_post(http, a, f"/agents/{a.state.agent_id}/heartbeat", big)
    assert r.status_code == 413
    r = http.post("/agents/enroll", content=big)
    assert r.status_code == 413


def test_decompression_bomb_is_refused(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    run = _run(gateway, a)
    bomb = crypto.seal(gateway.keys.identity.encryption_public, b"[" + b" " * (70 << 20) + b"]",
                       crypto.aad_for(a.state.agent_id, run, "0"))
    body = json.dumps({"seq": 0, "envelope": bomb}).encode()
    assert len(body) < 256 << 10                                   # tiny on the wire
    assert _signed_post(http, a, f"/agents/{a.state.agent_id}/runs/{run}/batches", body).status_code == 400


def test_revoked_agent_is_cut_off_mid_run(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp, "a")
    run = _run(gateway, a)
    gateway.revoke_agent(a.state.agent_id, ADMIN)
    path = f"/agents/{a.state.agent_id}/runs/{run}/batches"
    mcp = [make_mcp(DS, "status", {"removed": False})]
    assert _signed_post(http, a, path, _batch_body(gateway, a, run, 0, mcp)).status_code == 401


# --------------------------------------------------------------------------- what an agent may write
def _filter(mcps, lookup=None):
    pool = {"allowed_platforms": ["postgres"], "allowed_aspects": None}
    accepted, rejected = GatewayService.filter_mcps(mcps, pool, lookup)
    return accepted, rejected


def test_agents_cannot_rewrite_curated_vocabulary_or_people_edits():
    for mcp in (make_mcp("urn:li:glossaryTerm:Classification.PII.Email", "glossaryTermInfo", {"definition": "x"}),
                make_mcp("urn:li:domain:finance", "domainProperties", {"name": "x"}),
                make_mcp("urn:li:structuredProperty:io.cairn.certification.status", "propertyDefinition", {}),
                make_mcp("urn:li:dataProduct:c360", "dataProductProperties", {"assets": []}),
                make_mcp(DS, "editableSchemaMetadata", {"editableSchemaFieldInfo": []}),   # drop PII labels
                make_mcp(DS, "editableDatasetProperties", {"description": "x"}),
                make_mcp("urn:li:corpuser:admin", "corpUserInfo", {"active": True}),
                make_mcp("urn:li:dataHubPolicy:0", "dataHubPolicyInfo", {})):
        accepted, rejected = GatewayService.filter_mcps([mcp], {"allowed_platforms": []})   # even unrestricted pools
        assert accepted == [] and rejected, mcp["entityUrn"]


def test_platform_scope_cannot_be_escaped_through_urns_without_a_platform():
    smuggled = [make_mcp("urn:li:tag:pii-cleared", "tagProperties", {"name": "x"}),
                make_mcp("urn:li:dataJob:(urn:li:dataFlow:(airflow,etl,PROD),load)", "dataJobInfo", {}),
                make_mcp("urn:li:chart:(looker,1)", "chartInfo", {}),
                make_mcp(SNOWFLAKE_DB, "containerProperties", {"name": "x"}),       # stored as snowflake
                make_mcp("urn:li:container:unknown", "containerProperties", {"name": "x"})]
    stored = {(SNOWFLAKE_DB, "dataPlatformInstance"): {"platform": "urn:li:dataPlatform:snowflake"}}
    accepted, rejected = _filter(smuggled, lambda urn, aspect: stored.get((urn, aspect)))
    assert accepted == [] and len(rejected) == 5


def test_postgres_ingestion_output_is_accepted():
    batch = [make_mcp(PG_DB, "containerProperties", {"name": "finance"}),
             make_mcp(PG_DB, "dataPlatformInstance", {"platform": "urn:li:dataPlatform:postgres"}),
             make_mcp(DS, "schemaMetadata", {}),
             make_mcp(DS, "globalTags", {"tags": [{"tag": "urn:li:tag:pii"}]}),
             make_mcp(DS, "container", {"container": PG_DB}),
             make_mcp(f"urn:li:schemaField:({DS},email)", "schemaFieldKey", {})]
    accepted, rejected = _filter(batch)
    assert rejected == [] and len(accepted) == len(batch)
    # A later batch for the same container: the platform is known from what was stored.
    later = [make_mcp(PG_DB, "subTypes", {"typeNames": ["Database"]})]
    accepted, _ = _filter(later, lambda urn, aspect: {"platform": "urn:li:dataPlatform:postgres"}
                          if (urn, aspect) == (PG_DB, "dataPlatformInstance") else None)
    assert accepted == later


def test_view_definition_queries_are_scoped_by_their_subjects():
    q = "urn:li:query:view_abc"
    own = [make_mcp(q, "queryProperties", {"statement": {"value": "select 1", "language": "SQL"}, "source": "SYSTEM"}),
           make_mcp(q, "querySubjects", {"subjects": [{"entity": DS}]}),
           make_mcp(q, "status", {"removed": False})]
    accepted, rejected = _filter(own)
    assert rejected == [] and len(accepted) == 3
    foreign = [make_mcp(q, "querySubjects", {"subjects": [
        {"entity": "urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)"}]}), make_mcp(q, "status", {})]
    assert _filter(foreign)[0] == []
    # A later batch: the subjects are already stored.
    stored = lambda urn, aspect: {"subjects": [{"entity": DS}]} if aspect == "querySubjects" else None  # noqa: E731
    assert _filter([make_mcp(q, "status", {})], stored)[1] == []
    assert _filter([make_mcp(q, "status", {})])[0] == []                 # nothing proves the platform


def test_deletes_are_refused():
    mcp = {**make_mcp(DS, "schemaMetadata", {}), "changeType": "DELETE"}
    assert _filter([mcp])[0] == []


# --------------------------------------------------------------------------- audit integrity
def test_forwarded_for_is_only_trusted_from_configured_proxies():
    import ipaddress

    class Req:
        def __init__(self, peer, xff):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    assert _client_ip(Req("10.1.2.3", "1.2.3.4"), trusted=[]) == "10.1.2.3"            # agent chooses nothing
    assert _client_ip(Req("10.1.2.3", "1.2.3.4"), trusted=[ipaddress.ip_network("10.0.0.0/8")]) == "1.2.3.4"
    assert _client_ip(Req("192.168.1.1", "1.2.3.4"), trusted=[ipaddress.ip_network("10.0.0.0/8")]) == "192.168.1.1"
