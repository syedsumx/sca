"""Remote agent hardening (L44): an agent rotates its keys without downtime, and a gateway with mutual TLS refuses
an agent without a client certificate from its CA. Rotation runs over the gateway's HTTP surface; mTLS against a
real uvicorn server with real certificates."""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.agent import crypto
from cairn.agent.client import AgentState, GatewayClient
from cairn.agent.gateway import EnqueueRequest, PoolSpec, rotation_proof_bytes
from cairn.agent.worker import AgentConfig, AgentWorker
from cairn.app import create_gateway_app
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"


@pytest.fixture
def gw(settings, graph, tmp_path):
    services = build_services(settings, graph=graph, with_ai=False)
    services.gateway.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=4), ADMIN)
    return services, services.gateway, TestClient(create_gateway_app(services)), tmp_path


def _agent(gateway, http, tmp_path, name="a"):
    client = GatewayClient("http://testserver", AgentState(str(tmp_path / name)), http=http)
    client.enroll(gateway.create_enrollment_token("finance", ADMIN)["token"], hostname=name)
    return client


def _signed(http, ident, agent_id, path, body=b"{}"):
    return http.post(path, content=body, headers={**crypto.sign_request(ident, agent_id, "POST", path, body),
                                                  "Content-Type": "application/json"})


def test_an_agent_rotates_its_keys_without_downtime(gw):
    services, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp)
    old, aid = a.state.identity, a.state.agent_id
    hb = f"/agents/{aid}/heartbeat"
    out = a.rotate_keys()
    assert out["previous_key_valid_until_ms"] > out["rotated_at_ms"]
    assert a.state.identity.signing_public != old.signing_public and "next_identity" not in a.state.meta
    assert AgentState(str(tmp / "a")).identity.signing_public == a.state.identity.signing_public   # written down
    # Both keys sign during the grace period: requests already on their way go through.
    assert _signed(http, a.state.identity, aid, hb).status_code == 200
    assert _signed(http, old, aid, hb).status_code == 200
    # Tasks are sealed to the new key and open; one sealed to the old key in flight opens too.
    gateway.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"type": "postgres", "config": {}}}), ADMIN)
    assert a.poll()["recipe"]["source"]["type"] == "postgres"
    sealed = crypto.seal(old.encryption_public, b'{"task_id": "t1"}', crypto.aad_for(aid, "t1"))
    assert crypto.open_sealed(a.state.openers[-1].encryption_key, sealed, crypto.aad_for(aid, "t1"))
    # The retired key cannot start another rotation, even in its grace.
    new = crypto.Identity.generate()
    body = {"signing_public": new.signing_public, "encryption_public": new.encryption_public,
            "proof": crypto.sign_bytes(new, rotation_proof_bytes(aid, new.signing_public, new.encryption_public))}
    import json
    r = _signed(http, old, aid, f"/agents/{aid}/keys/rotate", json.dumps(body).encode())
    assert r.status_code == 401, r.text
    # After the grace period only the new key signs.
    gateway.key_grace_ms = 0
    assert _signed(http, old, aid, hb).status_code == 401
    assert _signed(http, a.state.identity, aid, hb).status_code == 200
    events = [e["action"] for e in services.audit.entries(limit=50)]
    assert "agent.keys_rotated" in events


def test_a_rotation_needs_proof_of_the_new_key_and_a_new_key(gw):
    import json

    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp)
    aid, cur = a.state.agent_id, a.state.identity
    path = f"/agents/{aid}/keys/rotate"
    new, other = crypto.Identity.generate(), crypto.Identity.generate()

    def ask(sp, ep, proof):
        return _signed(http, cur, aid, path, json.dumps({"signing_public": sp, "encryption_public": ep, "proof": proof}).encode())

    good = lambda i: crypto.sign_bytes(i, rotation_proof_bytes(aid, i.signing_public, i.encryption_public))  # noqa: E731
    assert ask(new.signing_public, new.encryption_public, good(other)).status_code == 400        # proof by another key
    assert ask(new.signing_public, other.encryption_public, good(new)).status_code == 400        # proof for other keys
    assert ask(cur.signing_public, cur.encryption_public, good(cur)).status_code == 400          # not a new key
    assert ask("AAAA", new.encryption_public, good(new)).status_code == 400                      # not a key
    # Unsigned, or by someone else's key: refused before anything else.
    stranger = crypto.Identity.generate()
    r = _signed(http, stranger, aid, path, json.dumps({"signing_public": new.signing_public,
                "encryption_public": new.encryption_public, "proof": good(new)}).encode())
    assert r.status_code == 401
    gateway.revoke_agent(aid, ADMIN)
    assert ask(new.signing_public, new.encryption_public, good(new)).status_code == 401           # revoked


def test_the_gateway_asks_old_keys_to_rotate_and_the_agent_does(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp)
    worker = AgentWorker(AgentConfig(gateway_url="http://testserver", state_dir=str(tmp / "a")), client=a)
    first = a.state.identity.signing_public
    assert a.heartbeat([])["rotate_key"] is False
    gateway.key_max_age_ms = 1
    time.sleep(0.01)
    reply = a.heartbeat([])
    assert reply["rotate_key"] is True
    worker.handle_heartbeat(reply)
    assert a.state.identity.signing_public != first
    gateway.key_max_age_ms = 30 * 86_400_000
    assert a.heartbeat([])["rotate_key"] is False                  # the new keys are fresh
    # The agent's own schedule rotates too.
    worker.config.key_rotate_days = 1e-9
    worker.handle_heartbeat({})
    assert a.state.identity.signing_public not in (first,)


def test_a_rotation_cut_short_by_a_crash_is_finished_or_dropped(gw):
    _, gateway, http, tmp = gw
    a = _agent(gateway, http, tmp)
    aid, cur = a.state.agent_id, a.state.identity
    # Written down, then the agent died before sending it: the gateway never took the new keys.
    a.state.meta["next_identity"] = crypto.Identity.generate().to_dict()
    a.state.save()
    restarted = GatewayClient("http://testserver", AgentState(str(tmp / "a")), http=http)
    assert restarted.finish_rotation() == "dropped"
    assert restarted.state.identity.signing_public == cur.signing_public and "next_identity" not in restarted.state.meta
    # Sent and taken, but the answer was lost: the new keys are made current on restart.
    new = crypto.Identity.generate()
    restarted.state.meta["next_identity"] = new.to_dict()
    restarted.state.save()
    import json
    proof = crypto.sign_bytes(new, rotation_proof_bytes(aid, new.signing_public, new.encryption_public))
    assert _signed(http, cur, aid, f"/agents/{aid}/keys/rotate", json.dumps({
        "signing_public": new.signing_public, "encryption_public": new.encryption_public, "proof": proof}).encode()).status_code == 200
    again = GatewayClient("http://testserver", AgentState(str(tmp / "a")), http=http)
    assert again.finish_rotation() == "completed"
    assert again.state.identity.signing_public == new.signing_public
    assert again.heartbeat([])["ok"]


# --------------------------------------------------------------------------- mutual TLS
def _certs(d):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def make(cn, issuer=None, issuer_key=None, ca=False, server=False):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        b = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer.subject if issuer else name)
             .public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
        if issuer is not None:
            b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        if ca:
            b = b.add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                              key_encipherment=False, data_encipherment=False, key_agreement=False,
                                              encipher_only=False, decipher_only=False), critical=True)
        if server:
            b = b.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        elif not ca:
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        cert = b.sign(issuer_key or key, hashes.SHA256())
        return cert, key

    def write(stem, cert, key):
        (d / f"{stem}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / f"{stem}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))

    ca, ca_key = make("cairn agents CA", ca=True)
    write("ca", ca, ca_key)
    write("server", *make("gateway", ca, ca_key, server=True))
    write("agent", *make("agent-a", ca, ca_key))
    other_ca, other_key = make("someone else", ca=True)
    write("stranger", *make("agent-x", other_ca, other_key))
    return {k: str(d / k) for k in ("ca.pem", "server.pem", "server.key", "agent.pem", "agent.key",
                                    "stranger.pem", "stranger.key")}


def test_a_gateway_with_mutual_tls_refuses_agents_without_its_client_certificate(settings, graph, tmp_path, monkeypatch):
    import uvicorn

    from cairn.cli import gateway_tls

    c = _certs(tmp_path)
    # Settings that cannot work stop the gateway at start.
    monkeypatch.setenv("CAIRN_GATEWAY_CLIENT_CA", c["ca.pem"])
    with pytest.raises(SystemExit, match="mTLS is TLS first"):
        gateway_tls()
    monkeypatch.setenv("CAIRN_GATEWAY_TLS_CERT", c["server.pem"])
    monkeypatch.setenv("CAIRN_GATEWAY_TLS_KEY", str(tmp_path / "missing.key"))
    with pytest.raises(SystemExit, match="no such file"):
        gateway_tls()
    monkeypatch.setenv("CAIRN_GATEWAY_TLS_KEY", c["server.key"])
    tls = gateway_tls()
    assert tls["ssl_cert_reqs"] == ssl.CERT_REQUIRED

    services = build_services(settings, graph=graph, with_ai=False)
    services.gateway.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"]), ADMIN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_gateway_app(services), host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=75, **tls))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    url = f"https://127.0.0.1:{port}"
    try:
        token = lambda: services.gateway.create_enrollment_token("finance", ADMIN)["token"]  # noqa: E731
        # An agent with a certificate from the gateway's CA enrols and works.
        ok = GatewayClient(url, AgentState(str(tmp_path / "ok")), verify=c["ca.pem"], cert=(c["agent.pem"], c["agent.key"]))
        ok.enroll(token(), hostname="ok")
        assert ok.heartbeat([])["ok"]
        ok.rotate_keys()
        assert ok.heartbeat([])["ok"]
        # Without one, or with one from another CA, the handshake fails: nothing reaches the gateway.
        for name, cert in (("none", None), ("stranger", (c["stranger.pem"], c["stranger.key"]))):
            bad = GatewayClient(url, AgentState(str(tmp_path / name)), verify=c["ca.pem"], cert=cert)
            with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
                bad.enroll(token(), hostname=name)
        assert [a["hostname"] for a in services.gateway.list_agents()] == ["ok"]
    finally:
        server.should_exit = True
