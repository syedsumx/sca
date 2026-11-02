"""Workload identity (cairn.identity.workload): which workload tokens are exchanged, and for whom."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from sqlalchemy import create_engine, select

from cairn.common.audit import AuditLog
from cairn.common.db import init_db
from cairn.dataaccess.trino_auth import TrinoTokens
from cairn.identity.workload import WorkloadIdentity, WorkloadRefused, parse_issuers, workload_bindings

ISS = "https://oidc.example.com"


@pytest.fixture
def world(tmp_path):
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    jwks = {"keys": [
        {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key())), "kid": "r1", "alg": "RS256", "use": "sig"},
        {**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(ec_key.public_key())), "kid": "e1", "alg": "ES256", "use": "sig"}]}
    (tmp_path / "jwks.json").write_text(json.dumps(jwks))
    issuers = parse_issuers(json.dumps([{"name": "ci", "issuer": ISS, "audience": "cairn",
                                         "jwks_file": str(tmp_path / "jwks.json"), "max_token_lifetime": 3600}]))
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    audit = AuditLog(engine)
    status = {}
    wi = WorkloadIdentity(issuers, engine, TrinoTokens(engine, "k" * 32, "cairn"), audit,
                          user_status=lambda u: status.get(u, "ACTIVE"))

    def token(sub="repo:acme/etl:ref:refs/heads/main", key="r1", life=600, **claims):
        now = int(time.time())
        body = {"iss": ISS, "aud": "cairn", "sub": sub, "iat": now, "nbf": now, "exp": now + life, **claims}
        priv, alg = (rsa_key, "RS256") if key == "r1" else (ec_key, "ES256")
        return jwt.encode(body, priv, algorithm=alg, headers={"kid": key})
    return wi, token, audit, status


def test_a_bound_workload_becomes_its_cairn_user(world):
    wi, token, audit, _ = world
    b = wi.bind("ci", "repo:acme/etl:ref:refs/heads/main", "svc-etl", "urn:li:corpuser:admin",
                {"repository": "acme/etl", "job_workflow_ref.branch": "main"})
    claims = {"repository": "acme/etl", "job_workflow_ref": {"branch": "main"}}
    for key in ("r1", "e1"):  # RSA and EC keys
        out = wi.exchange(token(key=key, **claims))
        assert out["user"] == "svc-etl"
        assert wi.principal_of(out["token"]) == "urn:li:corpuser:svc-etl"
    entries = [e for e in audit.entries("identity")]
    assert [e["action"] for e in entries].count("workload.token_issued") == 2
    assert entries[-1]["action"] == "workload.binding_created" and entries[-1]["details"]["binding"] == b["id"]
    # Conditions must all match: another repository's token with the same subject is refused.
    with pytest.raises(WorkloadRefused) as e:
        wi.exchange(token(repository="evil/etl", job_workflow_ref={"branch": "main"}))
    assert e.value.error == "access_denied"
    # Unbinding stops the exchange.
    assert wi.unbind(b["id"], "urn:li:corpuser:admin")
    with pytest.raises(WorkloadRefused):
        wi.exchange(token(**claims))


def test_tokens_that_are_refused(world, tmp_path):
    wi, token, _, status = world
    wi.bind("ci", "repo:acme/etl:ref:refs/heads/main", "svc-etl", "urn:li:corpuser:admin")
    now = int(time.time())
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = {
        "wrong audience": token(aud="kubernetes"),
        "wrong issuer": token(iss="https://evil.example.com"),
        "expired": token(life=-120),
        "lives too long": token(life=7200),
        "unknown key": token(key="r1")[:-10] + "AAAAAAAAAA",
        "no exp": jwt.encode({"iss": ISS, "aud": "cairn", "sub": "x", "iat": now}, other_key, "RS256", headers={"kid": "r1"}),
        "forged": jwt.encode({"iss": ISS, "aud": "cairn", "sub": "repo:acme/etl:ref:refs/heads/main", "iat": now,
                              "exp": now + 60}, other_key, "RS256", headers={"kid": "r1"}),
        "symmetric": jwt.encode({"iss": ISS, "aud": "cairn", "sub": "s", "iat": now, "exp": now + 60}, "k" * 32, "HS256",
                                headers={"kid": "r1"}),
        "unbound": token(sub="repo:acme/other:ref:refs/heads/main"),
        "garbage": "not.a.jwt",
    }
    for why, t in bad.items():
        with pytest.raises(WorkloadRefused):
            wi.exchange(t)
            pytest.fail(why)
    status["urn:li:corpuser:svc-etl"] = "SUSPENDED"
    with pytest.raises(WorkloadRefused, match="suspended"):
        wi.exchange(token())
    status["urn:li:corpuser:svc-etl"] = "ACTIVE"
    # Two users bound to one workload: refused until the bindings are fixed.
    wi.bind("ci", "repo:acme/etl:ref:refs/heads/main", "svc-other", "urn:li:corpuser:admin", {"x": "1"})
    with pytest.raises(WorkloadRefused, match="several"):
        wi.exchange(token(x="1"))
    assert wi.exchange(token(x="2"))["user"] == "svc-etl"


def test_only_cairns_own_workload_tokens_are_principals(world):
    wi, token, _, _ = world
    tokens = wi.tokens
    assert wi.principal_of(token()) is None                                  # someone else's JWT: not ours
    assert wi.principal_of(tokens.mint("urn:li:corpuser:ana")["token"]) is None  # a Trino token
    assert wi.principal_of(tokens.mint("urn:li:corpuser:ana", audience="cairn")["token"]) is None  # not a workload's
    expired = tokens.mint("urn:li:corpuser:svc", 60, audience="cairn", extra={"via": "workload"})["token"]
    claims = jwt.decode(expired, options={"verify_signature": False})
    assert claims["via"] == "workload"
    tokens.verify(expired, "cairn")
    import cairn.dataaccess.trino_auth as ta
    real = ta.time.time
    ta.time.time = lambda: real() + 3600
    try:
        with pytest.raises(ValueError, match="expired"):
            wi.principal_of(expired)
    finally:
        ta.time.time = real


def test_issuer_configuration_is_validated():
    ok = {"name": "k8s", "issuer": "https://kubernetes.default.svc", "audience": "cairn",
          "jwks_url": "https://kubernetes.default.svc/openid/v1/jwks"}
    assert parse_issuers(json.dumps([ok]))["k8s"].token_ttl == 3600
    for bad in ({**ok, "audience": "https://kubernetes.default.svc"},  # the cluster's own audience
                {**ok, "jwks_url": "http://idp.example.com/jwks"},       # keys over plain HTTP
                {**ok, "jwks_file": "/x"},                               # both
                {**ok, "algorithms": ["HS256"]},
                {**ok, "name": "Bad Name"},
                {**ok, "max_token_lifetime": 30 * 86400},
                {k: v for k, v in ok.items() if k != "audience"}):
        with pytest.raises(ValueError):
            parse_issuers(json.dumps([bad]))
    with pytest.raises(ValueError):
        parse_issuers(json.dumps([ok, ok]))  # names are unique


def test_binding_changes_commit_with_their_audit_entries(world):
    wi, _, audit, _ = world
    real = audit._append

    def boom(conn, body):
        raise RuntimeError("audit store failed")
    audit._append = boom
    with pytest.raises(RuntimeError):
        wi.bind("ci", "s", "svc-etl", "urn:li:corpuser:admin")
    audit._append = real
    with wi.engine.connect() as conn:
        assert conn.execute(select(workload_bindings)).first() is None
    with pytest.raises(ValueError):
        wi.bind("nope", "s", "svc-etl", "urn:li:corpuser:admin")  # unknown issuer
    wi.bind("ci", "s", "svc-etl", "urn:li:corpuser:admin")
    with pytest.raises(ValueError, match="already"):
        wi.bind("ci", "s", "svc-etl", "urn:li:corpuser:admin")
