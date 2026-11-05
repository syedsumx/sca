"""Secret scopes end to end: Cairn over HTTP, people and groups on the access list, notebooks reading through
cairn_lakehouse.secret, encryption at rest, key rotation, and values kept out of listings and the audit log."""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import time

import httpx
import pytest
from sqlalchemy import select, update

from cairn.governance.abac import Policy, PolicyBundle

ANA, BOB, CAROL, OPS = (f"urn:li:corpuser:{n}" for n in ("ana", "bob", "carol", "ops"))
DATA_TEAM = "urn:li:corpGroup:data-team"
VALUE = "sk_live_" + "x" * 40


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _stack(key: str | None, previous: str | None = None, db: str | None = None):
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    d = db or tempfile.mkdtemp(prefix="secrets-")
    env = {k: v for k, v in (("CAIRN_SECRETS_KEY", key), ("CAIRN_SECRETS_KEY_PREVIOUS", previous)) if v}
    saved = {k: os.environ.pop(k, None) for k in ("CAIRN_SECRETS_KEY", "CAIRN_SECRETS_KEY_PREVIOUS")}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        graph.aspects[(CAROL, "groupMembership")] = {"groups": [DATA_TEAM]}
        s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": f"{d}/keys"}),
                           graph=graph, with_ai=False, with_gateway=False)
    finally:
        for k in env:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v})
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ops", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    # Bearer tokens a notebook would carry, resolved by Cairn's notebook token hook.
    tokens = {"nb.ana.token": ANA, "nb.carol.token": CAROL, "nb.bob.token": BOB}
    s.identity.notebook_lookup = tokens.get
    p = _port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{p}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return s, server, url, d


def as_(who: str) -> dict:
    return {"X-Cairn-Actor": who}


@pytest.fixture(scope="module")
def stack():
    key = _key()
    s, server, url, d = _stack(key)
    yield s, url, key, d
    server.should_exit = True


def test_access_list_by_person_and_group(stack):
    s, url, _, _ = stack
    c = httpx.Client(base_url=url, timeout=10)
    assert c.post("/v1/secret-scopes", json={"name": "stripe", "description": "payments"}, headers=as_(ANA)).status_code == 201
    assert c.post("/v1/secret-scopes", json={"name": "stripe"}, headers=as_(BOB)).status_code == 409
    assert c.post("/v1/secret-scopes", json={"name": "../etc"}, headers=as_(ANA)).status_code == 400
    assert c.put("/v1/secret-scopes/stripe/secrets/api_key", json={"value": VALUE}, headers=as_(ANA)).status_code == 200

    # Nobody else sees the scope, or learns that it exists.
    for path in ("/v1/secret-scopes/stripe", "/v1/secret-scopes/stripe/secrets/api_key"):
        assert c.get(path, headers=as_(BOB)).status_code == 404
    assert "stripe" not in [x["name"] for x in c.get("/v1/secret-scopes", headers=as_(BOB)).json()]

    # A group on the list: its members read, but may not write or change the list.
    assert c.put("/v1/secret-scopes/stripe/acl", json={"principal": DATA_TEAM, "permission": "READ"},
                 headers=as_(ANA)).status_code == 200
    r = c.get("/v1/secret-scopes/stripe/secrets/api_key", headers=as_(CAROL))
    assert r.status_code == 200 and r.json()["value"] == VALUE and r.headers["cache-control"] == "no-store"
    assert c.put("/v1/secret-scopes/stripe/secrets/api_key", json={"value": "x"}, headers=as_(CAROL)).status_code == 403
    assert c.put("/v1/secret-scopes/stripe/acl", json={"principal": BOB, "permission": "READ"},
                 headers=as_(CAROL)).status_code == 403
    assert c.get("/v1/secret-scopes/stripe/secrets/api_key", headers=as_(BOB)).status_code == 404

    # WRITE: change and remove secrets, not the list.
    assert c.put("/v1/secret-scopes/stripe/acl", json={"principal": BOB, "permission": "WRITE"},
                 headers=as_(ANA)).status_code == 200
    assert c.put("/v1/secret-scopes/stripe/secrets/webhook", json={"value": "whsec_1"}, headers=as_(BOB)).status_code == 200
    assert c.delete("/v1/secret-scopes/stripe/secrets/webhook", headers=as_(BOB)).status_code == 204
    assert c.delete("/v1/secret-scopes/stripe", headers=as_(BOB)).status_code == 403

    # The last manager cannot leave; removing someone takes their access away.
    assert c.put("/v1/secret-scopes/stripe/acl", json={"principal": ANA, "permission": None},
                 headers=as_(ANA)).status_code == 400
    assert c.put("/v1/secret-scopes/stripe/acl", json={"principal": DATA_TEAM, "permission": None},
                 headers=as_(ANA)).status_code == 200
    assert c.get("/v1/secret-scopes/stripe/secrets/api_key", headers=as_(CAROL)).status_code == 404


def test_values_never_listed_logged_or_stored_in_clear(stack):
    s, url, _, _ = stack
    c = httpx.Client(base_url=url, timeout=10)
    c.post("/v1/secret-scopes", json={"name": "warehouse"}, headers=as_(ANA))
    c.put("/v1/secret-scopes/warehouse/secrets/password", json={"value": VALUE}, headers=as_(ANA))
    c.get("/v1/secret-scopes/warehouse/secrets/password", headers=as_(ANA))
    listing = c.get("/v1/secret-scopes/warehouse", headers=as_(ANA)).json()
    assert [x["key"] for x in listing["secrets"]] == ["password"] and VALUE not in json.dumps(listing)
    from cairn.identity.secret_scopes import secrets_table

    with s.engine.connect() as conn:
        stored = conn.execute(select(secrets_table.c.ciphertext).where(secrets_table.c.scope == "warehouse")).scalar_one()
    assert VALUE not in stored and VALUE.encode() not in base64.b64decode(stored)
    entries = json.dumps(s.audit.entries(limit=500), default=str)
    assert VALUE not in entries
    assert '"secret.read"' in entries and "secret-scope:warehouse" in entries


def test_operator_manages_but_reads_only_after_granting_themselves(stack):
    s, url, _, _ = stack
    c = httpx.Client(base_url=url, timeout=10)
    c.post("/v1/secret-scopes", json={"name": "ops-only"}, headers=as_(ANA))
    c.put("/v1/secret-scopes/ops-only/secrets/token", json={"value": VALUE}, headers=as_(ANA))
    assert "ops-only" in [x["name"] for x in c.get("/v1/secret-scopes", headers=as_(OPS)).json()]
    assert c.get("/v1/secret-scopes/ops-only", headers=as_(OPS)).status_code == 200
    assert c.get("/v1/secret-scopes/ops-only/secrets/token", headers=as_(OPS)).status_code == 404
    assert c.put("/v1/secret-scopes/ops-only/acl", json={"principal": OPS, "permission": "READ"},
                 headers=as_(OPS)).status_code == 200
    assert c.get("/v1/secret-scopes/ops-only/secrets/token", headers=as_(OPS)).json()["value"] == VALUE
    changes = [e for e in s.audit.entries(limit=500) if e.get("action")
               == "secret.acl_changed"]
    assert changes
    # Others may not rotate the key.
    assert c.post("/v1/secret-scopes/rotate", headers=as_(ANA)).status_code == 403


def test_tampered_or_moved_ciphertext_is_refused(stack):
    s, url, _, _ = stack
    c = httpx.Client(base_url=url, timeout=10)
    c.post("/v1/secret-scopes", json={"name": "tamper"}, headers=as_(ANA))
    c.put("/v1/secret-scopes/tamper/secrets/a", json={"value": "first"}, headers=as_(ANA))
    c.put("/v1/secret-scopes/tamper/secrets/b", json={"value": "second"}, headers=as_(ANA))
    from cairn.identity.secret_scopes import secrets_table

    t = secrets_table
    with s.engine.begin() as conn:
        a = conn.execute(select(t.c.ciphertext).where(t.c.scope == "tamper", t.c.key == "a")).scalar_one()
        conn.execute(update(t).where(t.c.scope == "tamper", t.c.key == "b").values(ciphertext=a))   # moved
    assert c.get("/v1/secret-scopes/tamper/secrets/b", headers=as_(ANA)).status_code == 500
    raw = bytearray(base64.b64decode(a))
    raw[-1] ^= 1
    with s.engine.begin() as conn:
        conn.execute(update(t).where(t.c.scope == "tamper", t.c.key == "a")
                     .values(ciphertext=base64.b64encode(bytes(raw)).decode()))
    assert c.get("/v1/secret-scopes/tamper/secrets/a", headers=as_(ANA)).status_code == 500


def test_notebook_reads_through_the_client_as_its_person(stack, monkeypatch):
    import sys

    monkeypatch.syspath_prepend(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lakehouse-client", "src"))
    sys.modules.pop("cairn_lakehouse", None)
    from cairn_lakehouse import CairnLakehouseError, secret

    _, url, _, _ = stack
    c = httpx.Client(base_url=url, timeout=10)
    c.post("/v1/secret-scopes", json={"name": "nb"}, headers=as_(ANA))
    c.put("/v1/secret-scopes/nb/secrets/db_password", json={"value": VALUE}, headers=as_(ANA))
    monkeypatch.setenv("CAIRN_URL", url)
    monkeypatch.setenv("CAIRN_TOKEN", "nb.ana.token")
    assert secret("nb", "db_password") == VALUE
    monkeypatch.setenv("CAIRN_TOKEN", "nb.bob.token")
    with pytest.raises(CairnLakehouseError, match="404"):
        secret("nb", "db_password")


def test_rotation_and_no_key():
    old, new = _key(), _key()
    s1, server1, url1, d = _stack(old)
    c = httpx.Client(base_url=url1, timeout=10)
    c.post("/v1/secret-scopes", json={"name": "rot"}, headers=as_(ANA))
    for k in ("a", "b", "c"):
        c.put(f"/v1/secret-scopes/rot/secrets/{k}", json={"value": f"v-{k}"}, headers=as_(ANA))
    server1.should_exit = True

    # The new key alone cannot read what the old one sealed.
    s2, server2, url2, _ = _stack(new, db=d)
    assert httpx.get(f"{url2}/v1/secret-scopes/rot/secrets/a", headers=as_(ANA)).status_code == 500
    server2.should_exit = True

    # New key with the old one as previous: reads, then rotation re-encrypts everything with the new key.
    s3, server3, url3, _ = _stack(new, previous=old, db=d)
    c3 = httpx.Client(base_url=url3, timeout=10)
    assert c3.get("/v1/secret-scopes/rot/secrets/a", headers=as_(ANA)).json()["value"] == "v-a"
    assert c3.post("/v1/secret-scopes/rotate", headers=as_(OPS)).json() == {"reencrypted": 3}
    assert c3.post("/v1/secret-scopes/rotate", headers=as_(OPS)).json() == {"reencrypted": 0}
    server3.should_exit = True

    s4, server4, url4, _ = _stack(new, db=d)                      # the old key can now go
    assert httpx.get(f"{url4}/v1/secret-scopes/rot/secrets/c", headers=as_(ANA)).json()["value"] == "v-c"
    server4.should_exit = True

    # Without a key the feature is off, never unencrypted.
    s5, server5, url5, _ = _stack(None, db=d)
    assert httpx.post(f"{url5}/v1/secret-scopes", json={"name": "x"}, headers=as_(ANA)).status_code == 503
    assert httpx.put(f"{url5}/v1/secret-scopes/rot/secrets/a", json={"value": "y"}, headers=as_(ANA)).status_code == 503
    server5.should_exit = True


def test_wrong_size_key_refuses_to_start():
    from cairn.identity.secret_scopes import SecretScopes

    os.environ["CAIRN_SECRETS_KEY"] = base64.b64encode(b"short").decode()
    try:
        with pytest.raises(RuntimeError, match="32 bytes"):
            SecretScopes(None, lambda u: [], lambda u: False)
    finally:
        os.environ.pop("CAIRN_SECRETS_KEY")
