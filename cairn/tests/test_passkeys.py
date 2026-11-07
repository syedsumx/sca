"""Passkeys and step-up, end to end over HTTP: a software authenticator (a P-256 key, as a security key or a phone
holds) registers a passkey and signs step-up challenges, checked by the webauthn library; detokenising and sharing
are refused without a fresh step-up and allowed with one; a stolen session cannot add its own passkey; replayed
challenges, someone else's step-up, an expired step-up and a cloned authenticator (its counter going back) are
refused."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

import pytest

pytest.importorskip("webauthn")
cbor2 = pytest.importorskip("cbor2")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from starlette.requests import Request  # noqa: E402  (module level: FastAPI resolves annotations here)
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

RP, ORIGIN = "cairn.example.com", "https://cairn.example.com"
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """A passkey as a device keeps it: a P-256 key, a credential id and a signature counter."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred = os.urandom(16)
        self.count = 0

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        return hashlib.sha256(RP.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + attested

    def create(self, options: dict) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        attested = b"\0" * 16 + struct.pack(">H", len(self.cred)) + self.cred + cose
        client = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": ORIGIN}).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(0x45, attested)})  # UP|UV|AT
        return {"id": b64u(self.cred), "rawId": b64u(self.cred), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "attestationObject": b64u(att)}}

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
        self.count += 1
        data = self._auth_data(0x05)                                            # UP|UV
        client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        sig = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred), "rawId": b64u(self.cred), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(data), "signature": b64u(sig)}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from cairn.common.audit import AuditLog
    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.governance.tokens import Tokens, build_tokens_router
    from cairn.identity.passkeys import Passkeys, build_passkeys_router

    monkeypatch.setenv("CAIRN_PASSKEY_RP_ID", RP)
    monkeypatch.setenv("CAIRN_PASSKEY_ORIGIN", ORIGIN)
    monkeypatch.setenv("CAIRN_TOKENS_KEY", base64.b64encode(os.urandom(32)).decode())
    engine = create_engine(f"sqlite:///{tmp_path}/pk.db")
    init_db(engine)
    clock = {"now": 1_800_000_000_000}
    told = []
    pk = Passkeys(engine, AuditLog(engine), lambda user, text: told.append((user, text)), clock=lambda: clock["now"])
    tokens = Tokens(engine, AuditLog(engine), lambda actor, priv, urn: True)
    tokens.step_up = pk.require
    tokens.create_domain(ANA, "email")

    def who(request: Request):
        return Principal(request.headers["x-user"], "token")
    app = FastAPI()
    app.include_router(build_passkeys_router(pk, who))
    app.include_router(build_tokens_router(tokens, who, lambda p, priv, urn, raise_on_deny=True: True))
    client = TestClient(app)
    return {"c": client, "pk": pk, "tokens": tokens, "clock": clock, "told": told}


def call(api, method, path, user=ANA, step_up=None, **kw):
    headers = {"x-user": user, **({"x-cairn-step-up": step_up} if step_up else {})}
    return api["c"].request(method, path, headers=headers, **kw)


def enrol(api, dev, user=ANA, step_up=None, name="laptop"):
    r = call(api, "POST", "/v1/passkeys/register/options", user, step_up)
    assert r.status_code == 200, r.text
    o = r.json()
    return call(api, "POST", "/v1/passkeys/register", user, step_up,
                json={"challenge_id": o["challenge_id"], "credential": dev.create(o["options"]), "name": name})


def step_up(api, dev, user=ANA, origin=ORIGIN):
    o = call(api, "POST", "/v1/passkeys/step-up/options", user).json()
    return call(api, "POST", "/v1/passkeys/step-up", user,
                json={"challenge_id": o["challenge_id"], "credential": dev.get(o["options"], origin)})


def test_a_passkey_is_registered_and_steps_up_sensitive_actions(api):
    dev = Authenticator()
    token = api["tokens"].tokenize("email", ["jane@acme.com"])[0]
    body = {"tokens": [token], "purpose": "fraud case 4471 review"}
    r = call(api, "POST", "/governance/tokens/domains/email/detokenize", json=body)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("step-up")   # not enrolled: refused
    assert call(api, "POST", "/v1/passkeys/step-up/options").status_code == 409             # register first
    r = enrol(api, dev)
    assert r.status_code == 201, r.text
    assert api["told"] and api["told"][0][0] == ANA                                        # told of the new passkey
    assert [p["name"] for p in call(api, "GET", "/v1/passkeys").json()["passkeys"]] == ["laptop"]
    # Without a step-up: refused. With one: the values come back.
    assert call(api, "POST", "/governance/tokens/domains/email/detokenize", json=body).status_code == 401
    up = step_up(api, dev)
    assert up.status_code == 200, up.text
    t = up.json()["step_up_token"]
    r = call(api, "POST", "/governance/tokens/domains/email/detokenize", step_up=t, json=body)
    assert r.status_code == 200 and r.json()["values"] == ["jane@acme.com"]
    # Someone else's step-up, an expired one, and a made-up one are refused.
    assert call(api, "POST", "/governance/tokens/domains/email/detokenize", BOB, t, json=body).status_code == 401
    api["clock"]["now"] += 301_000
    assert call(api, "POST", "/governance/tokens/domains/email/detokenize", step_up=t, json=body).status_code == 401
    assert call(api, "POST", "/governance/tokens/domains/email/detokenize", step_up="nope", json=body).status_code == 401


def test_a_stolen_session_cannot_add_its_own_passkey(api):
    mine, thief = Authenticator(), Authenticator()
    assert enrol(api, mine).status_code == 201
    r = call(api, "POST", "/v1/passkeys/register/options")                 # ana's session, no step-up
    assert r.status_code == 401
    t = step_up(api, mine).json()["step_up_token"]
    assert enrol(api, thief, step_up=t, name="phone").status_code == 201    # with ana's own passkey: allowed
    pid = call(api, "GET", "/v1/passkeys").json()["passkeys"][1]["id"]
    assert call(api, "DELETE", f"/v1/passkeys/{pid}").status_code == 401    # removing also needs one
    assert call(api, "DELETE", f"/v1/passkeys/{pid}", step_up=t).status_code == 204


def test_replays_wrong_origins_and_cloned_authenticators_are_refused(api):
    dev = Authenticator()
    enrol(api, dev)
    o = call(api, "POST", "/v1/passkeys/step-up/options").json()
    cred = dev.get(o["options"])
    assert call(api, "POST", "/v1/passkeys/step-up", json={"challenge_id": o["challenge_id"], "credential": cred}).status_code == 200
    replay = call(api, "POST", "/v1/passkeys/step-up", json={"challenge_id": o["challenge_id"], "credential": cred})
    assert replay.status_code == 400 and "used or expired" in replay.text   # a challenge is good once
    assert step_up(api, dev, origin="https://evil.example").status_code == 401  # a phishing site's origin
    dev.count = 0                                                           # a clone: its counter goes back
    assert step_up(api, dev).status_code == 401
    # Bob cannot use Ana's passkey.
    o = call(api, "POST", "/v1/passkeys/step-up/options", BOB)
    assert o.status_code == 409
