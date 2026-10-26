import json

import pytest
from fastapi.testclient import TestClient

from cairn.agent import crypto
from cairn.agent.client import AgentState, GatewayClient, GatewayHTTPError
from cairn.agent.dispatcher import ExecutionRequestDispatcher
from cairn.agent.gateway import EnqueueRequest, GatewayError, GatewayKeys, GatewayService, PoolSpec, find_inline_secrets
from cairn.agent.secrets import SecretNotFound, SecretResolver
from cairn.agent.sink import get_channel
from cairn.agent.worker import AgentConfig, AgentWorker
from cairn.app import create_gateway_app
from cairn.common.proposals import make_mcp
from cairn.services import build_services

from conftest import DS

ADMIN = "urn:li:corpuser:admin"


# --------------------------------------------------------------------------- crypto
def test_seal_roundtrip_and_tamper_detection():
    gw = crypto.Identity.generate()
    msg = json.dumps([{"x": 1}] * 100).encode()
    sealed = crypto.seal(gw.encryption_public, msg, b"agent|run|0")
    assert crypto.open_sealed(gw.encryption_key, sealed, b"agent|run|0") == msg
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(gw.encryption_key, sealed, b"agent|run|1")  # replay into another seq
    tampered = dict(sealed, ct=sealed["ct"][:-4] + ("AAAA" if not sealed["ct"].endswith("AAAA") else "BBBB"))
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(gw.encryption_key, tampered, b"agent|run|0")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(crypto.Identity.generate().encryption_key, sealed, b"agent|run|0")


def test_signatures():
    ident = crypto.Identity.generate()
    h = crypto.sign_request(ident, "agt_1", "POST", "/agents/agt_1/heartbeat", b"{}")
    crypto.verify_signature(ident.signing_public, "POST", "/agents/agt_1/heartbeat", h[crypto.HEADER_TS],
                            h[crypto.HEADER_NONCE], b"{}", h[crypto.HEADER_SIG])
    with pytest.raises(crypto.CryptoError):
        crypto.verify_signature(ident.signing_public, "POST", "/agents/agt_1/heartbeat", h[crypto.HEADER_TS],
                                h[crypto.HEADER_NONCE], b'{"x":1}', h[crypto.HEADER_SIG])


def test_inline_secret_detection():
    recipe = {"source": {"type": "postgres", "config": {"username": "u", "password": "hunter2",
                                                        "options": {"api_key": "${KEY}"}}}}
    assert find_inline_secrets(recipe) == ["source.config.password"]
    recipe["source"]["config"]["password"] = "${PG_PASSWORD}"
    assert find_inline_secrets(recipe) == []


def test_secret_resolver(tmp_path, monkeypatch):
    (tmp_path / "PG_PASSWORD").write_text("from-file\n")
    monkeypatch.setenv("SF_TOKEN", "from-env")
    r = SecretResolver.from_config([{"type": "env"}, {"type": "directory", "path": str(tmp_path)}])
    out = r.resolve({"a": "${SF_TOKEN}", "b": ["x-${PG_PASSWORD}-y"], "c": 3})
    assert out == {"a": "from-env", "b": ["x-from-file-y"], "c": 3}
    with pytest.raises(SecretNotFound):
        r.resolve("${NOPE}")


# --------------------------------------------------------------------------- end-to-end protocol
@pytest.fixture
def stack(settings, graph, tmp_path):
    services = build_services(settings, graph=graph, with_ai=False)
    gw = services.gateway
    gw.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=1), ADMIN)
    http = TestClient(create_gateway_app(services))
    return services, gw, http, tmp_path


def _enrolled_client(gw, http, tmp_path, name="a1"):
    token = gw.create_enrollment_token("finance", ADMIN)["token"]
    state = AgentState(str(tmp_path / name))
    client = GatewayClient("http://testserver", state, http=http)
    client.enroll(token, hostname=f"host-{name}")
    return client, token


def test_full_ingestion_run(stack):
    services, gw, http, tmp = stack
    client, token = _enrolled_client(gw, http, tmp)
    assert AgentState(str(tmp / "a1")).enrolled  # identity persisted
    with pytest.raises(GatewayHTTPError) as e:  # one-time token
        GatewayClient("http://testserver", AgentState(str(tmp / "a2")), http=http).enroll(token)
    assert e.value.status == 401

    er = "urn:li:dataHubExecutionRequest:abc"
    gw.enqueue(EnqueueRequest(pool="finance", execution_request_urn=er, recipe={
        "source": {"type": "postgres", "config": {"host_port": "db:5432", "password": "${PG_PASSWORD}"}}}), ADMIN)
    assert client.heartbeat([])["ok"]

    seen_recipes = []

    def fake_runner(recipe):
        seen_recipes.append(recipe)
        channel = get_channel(recipe["sink"]["config"]["run_id"])
        for i in range(7):
            channel.add(make_mcp(f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)", "status", {"removed": False}))
        channel.add(make_mcp("urn:li:dataset:(urn:li:dataPlatform:snowflake,evil,PROD)", "status", {"removed": False}))
        channel.add(make_mcp("urn:li:corpuser:ceo", "corpUserInfo", {"active": True}))
        return {"source": {"events_produced": 9}}

    import os
    os.environ["PG_PASSWORD"] = "s3cret"
    worker = AgentWorker(AgentConfig(gateway_url="http://testserver", state_dir=str(tmp / "a1"), batch_max_items=3),
                         runner=fake_runner, client=client)
    worker.tick()
    worker.pool.shutdown(wait=True)

    assert seen_recipes[0]["source"]["config"]["password"] == "s3cret"  # resolved locally only
    assert seen_recipes[0]["sink"]["type"] == "cairn-encrypted"
    runs = gw.list_runs()
    assert runs[0]["status"] == "SUCCESS" and runs[0]["mcps_forwarded"] == 7 and runs[0]["mcps_rejected"] == 2
    forwarded = [m["entityUrn"] for m in graph_emitted(services)]
    assert not any("snowflake" in u or "corpuser" in u for u in forwarded)
    result = services.graph.get_aspect(er, "dataHubExecutionRequestResult")
    assert result["status"] == "SUCCESS" and json.loads(result["report"])["mcps_sent"] == 9
    # The gateway-side task never held the plaintext secret.
    assert "s3cret" not in json.dumps(gw.list_runs()) and "s3cret" not in json.dumps(services.graph.emitted)


def graph_emitted(services):
    return [m for m in services.graph.emitted if m["aspectName"] == "status"]


def test_replay_skew_and_revocation(stack):
    services, gw, http, tmp = stack
    client, _ = _enrolled_client(gw, http, tmp)
    path = f"/agents/{client.state.agent_id}/heartbeat"
    body = b'{"running_task_ids":[]}'
    headers = crypto.sign_request(client.state.identity, client.state.agent_id, "POST", path, body)
    assert http.post(path, content=body, headers=headers).status_code == 200
    assert http.post(path, content=body, headers=headers).status_code == 401  # nonce replay
    old = dict(crypto.sign_request(client.state.identity, client.state.agent_id, "POST", path, body))
    old[crypto.HEADER_TS] = str(int(old[crypto.HEADER_TS]) - 3600)
    assert http.post(path, content=body, headers=old).status_code == 401  # skew (and bad sig)
    other, _ = _enrolled_client(gw, http, tmp, "a3")
    forged = crypto.sign_request(other.state.identity, client.state.agent_id, "POST", path, body)
    assert http.post(path, content=body, headers=forged).status_code == 401  # wrong key
    gw.revoke_agent(client.state.agent_id, ADMIN)
    with pytest.raises(GatewayHTTPError) as e:
        client.heartbeat([])
    assert e.value.status == 401


def test_sequence_and_lease_rules(stack):
    services, gw, http, tmp = stack
    client, _ = _enrolled_client(gw, http, tmp)
    gw.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"type": "postgres", "config": {}}}, max_attempts=2), ADMIN)
    gw.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"type": "postgres", "config": {}}}), ADMIN)
    task = client.poll()
    assert task and client.poll() is None  # pool max_concurrency=1
    run = task["run_id"]
    client.upload_batch(run, 0, [])
    with pytest.raises(GatewayHTTPError) as e:
        client.upload_batch(run, 0, [])
    assert e.value.status == 409
    # Claims SUCCESS but gateway saw seq 0 only → downgraded to FAILURE and retried.
    out = client.complete(run, "SUCCESS", {}, final_seq=5)
    assert out["status"] == "QUEUED"
    again = client.poll()
    assert again["task_id"] == task["task_id"] and again["run_id"] != run
    with pytest.raises(GatewayHTTPError):
        client.upload_batch(run, 1, [])  # old run id is dead


def test_enqueue_rejects_inline_credentials(stack):
    _, gw, _, _ = stack
    with pytest.raises(GatewayError) as e:
        gw.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"config": {"password": "plain"}}}), ADMIN)
    assert e.value.status == 422


def test_dispatcher_claims_pool_execution_requests(stack):
    services, gw, _, _ = stack
    d = ExecutionRequestDispatcher(gw, services.graph)
    recipe = json.dumps({"source": {"type": "postgres", "config": {"password": "${PG}"}},
                         "sink": {"type": "datahub-rest", "config": {"server": "http://core"}}})
    services.graph.aspects[("urn:li:dataHubExecutionRequest:1", "dataHubExecutionRequestInput")] = {
        "task": "RUN_INGEST", "executorId": "finance", "args": {"recipe": recipe}, "requestedAt": 0,
        "source": {"type": "SCHEDULED_INGESTION_SOURCE"}}
    services.graph.aspects[("urn:li:dataHubExecutionRequest:2", "dataHubExecutionRequestInput")] = {
        "task": "RUN_INGEST", "executorId": "default", "args": {"recipe": recipe}, "requestedAt": 0,
        "source": {"type": "MANUAL_INGESTION_SOURCE"}}
    assert d.poll_once() == 1
    assert d.poll_once() == 0  # idempotent
    runs = gw.list_runs()
    assert len(runs) == 1 and runs[0]["execution_request_urn"] == "urn:li:dataHubExecutionRequest:1"


def test_filter_mcps_policy():
    pool = {"allowed_platforms": ["postgres"], "allowed_aspects": None}
    ok = make_mcp(DS, "schemaMetadata", {})
    bad_platform = make_mcp("urn:li:dataset:(urn:li:dataPlatform:mysql,x,PROD)", "status", {})
    bad_entity = make_mcp("urn:li:dataHubPolicy:x", "dataHubPolicyInfo", {})
    accepted, rejected = GatewayService.filter_mcps([ok, bad_platform, bad_entity], pool)
    assert accepted == [ok] and len(rejected) == 2
    accepted, rejected = GatewayService.filter_mcps([bad_entity], {"allowed_platforms": []})
    assert accepted == []  # protected entity types are blocked even without a platform allowlist


def test_gateway_keys_persist(tmp_path):
    a = GatewayKeys(str(tmp_path))
    b = GatewayKeys(str(tmp_path))
    assert a.identity.encryption_public == b.identity.encryption_public
    import os
    import stat
    assert stat.S_IMODE(os.stat(a.path).st_mode) == 0o600
