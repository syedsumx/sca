"""SAML sign-in against a real identity provider (Keycloak, in Docker), handed on as OIDC: the test plays the browser
through Keycloak's login page, posts the signed response to Cairn, and redeems the code as an OIDC client would
(Cairn Core does the same). Forged, tampered, replayed and misdirected responses are refused; OIDC codes, PKCE
and client secrets are checked; the ID token verifies against Cairn's JWKS and carries the user's groups."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

IMAGE = os.environ.get("CAIRN_KEYCLOAK_IMAGE", "keycloak/keycloak:26.0.7")
# The saml extra (signxml, lxml): installed in CI's trino job, which runs this test; skipped where it is not.
pytest.importorskip("signxml", reason="needs the saml extra")
pytest.importorskip("lxml", reason="needs the saml extra")
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
REDIRECT = "http://127.0.0.1:9/callback/oidc"


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def keycloak():
    name, p = f"cairn-keycloak-{os.getpid()}", port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:8080",
                    "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=admin-test-only",
                    IMAGE, "start-dev"], check=True, capture_output=True, timeout=300)
    base = f"http://127.0.0.1:{p}"
    try:
        end = time.time() + 240
        while True:
            try:
                if httpx.get(f"{base}/realms/master", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < end, subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout[-3000:]
            time.sleep(2)
        yield base
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def admin(base: str) -> httpx.Client:
    t = httpx.post(f"{base}/realms/master/protocol/openid-connect/token", timeout=30, data={
        "grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "admin-test-only"}).json()
    return httpx.Client(base_url=f"{base}/admin/realms", headers={"Authorization": f"Bearer {t['access_token']}"}, timeout=30)


def realm(base: str, sp_entity: str, acs: str, sign_response: bool) -> None:
    kc = admin(base)
    assert kc.post("", json={"realm": "acme", "enabled": True}).status_code in (201, 409)
    kc.post("/acme/groups", json={"name": "finance"})
    kc.post("/acme/users", json={"username": "jane", "enabled": True, "email": "jane@acme.example", "emailVerified": True,
                                 "firstName": "Jane", "lastName": "Doe", "requiredActions": [],
                                 "credentials": [{"type": "password", "value": "jane-pass", "temporary": False}]})
    uid = kc.get("/acme/users", params={"username": "jane"}).json()[0]["id"]
    gid = next(g["id"] for g in kc.get("/acme/groups").json() if g["name"] == "finance")
    kc.put(f"/acme/users/{uid}/groups/{gid}")
    for c in kc.get("/acme/clients", params={"clientId": sp_entity}).json():
        kc.delete(f"/acme/clients/{c['id']}")
    r = kc.post("/acme/clients", json={
        "clientId": sp_entity, "protocol": "saml", "enabled": True, "redirectUris": [acs], "frontchannelLogout": False,
        "attributes": {"saml.assertion.signature": "false" if sign_response else "true",
                       "saml.server.signature": "true" if sign_response else "false",
                       "saml.client.signature": "false", "saml.force.name.id.format": "true",
                       "saml_name_id_format": "username", "saml_assertion_consumer_url_post": acs,
                       "saml.signature.algorithm": "RSA_SHA256"},
        "protocolMappers": [
            {"name": "groups", "protocol": "saml", "protocolMapper": "saml-group-membership-mapper",
             "config": {"attribute.name": "groups", "full.path": "false", "single": "false"}},
            {"name": "email", "protocol": "saml", "protocolMapper": "saml-user-property-mapper",
             "config": {"user.attribute": "email", "attribute.name": "email"}}]})
    assert r.status_code == 201, r.text


@pytest.fixture
def cairn(keycloak, settings, graph, tmp_path, monkeypatch):
    import uvicorn

    from cairn.app import create_api_app
    from cairn.services import build_services

    p = port()
    issuer = f"http://127.0.0.1:{p}/v1/sso"
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps([{"client_id": "core", "client_secret": "core-secret", "redirect_uris": [REDIRECT]}]))
    monkeypatch.setenv("CAIRN_SSO_ISSUER_URL", issuer)
    monkeypatch.setenv("CAIRN_SSO_SIGNING_PASSPHRASE", "sso-passphrase-test")
    monkeypatch.setenv("CAIRN_SSO_CLIENTS_FILE", str(clients))
    monkeypatch.setenv("CAIRN_SAML_IDP_METADATA_URL", f"{keycloak}/realms/acme/protocol/saml/descriptor")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            assert time.time() < end
            time.sleep(0.1)
    yield s, issuer
    server.should_exit = True


def sign_in(issuer: str, client_id: str = "core") -> dict:
    """Plays the browser: Cairn → Keycloak's login form → the signed response Keycloak posts back (not posted yet)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, nonce = secrets.token_hex(8), secrets.token_hex(8)
    b = httpx.Client(timeout=30, follow_redirects=False)
    r = b.get(f"{issuer}/authorize", params={"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
                                             "scope": "openid profile email", "state": state, "nonce": nonce,
                                             "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 302, r.text
    # Keycloak's session cookies are Secure; a browser sends them to 127.0.0.1 (a secure context), httpx does
    # not over plain HTTP, so they are sent the way the browser would.
    jar = lambda: {"Cookie": "; ".join(f"{c.name}={c.value}" for c in b.cookies.jar)}  # noqa: E731
    r = b.get(r.headers["location"])
    while r.status_code in (302, 303):
        r = b.get(r.headers["location"], headers=jar())
    action = html.unescape(re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', r.text).group(1))
    r = b.post(action, data={"username": "jane", "password": "jane-pass", "credentialId": ""}, headers=jar())
    fields = dict(re.findall(r'name="(SAMLResponse|RelayState)"\s+value="([^"]*)"', r.text))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style|head)[^>]*>.*?</\1>", "", r.text)))
    assert "SAMLResponse" in fields, (r.status_code, text[:600])
    return {"saml": html.unescape(fields["SAMLResponse"]), "relay": html.unescape(fields["RelayState"]),
            "verifier": verifier, "state": state, "nonce": nonce}


def post_acs(issuer: str, saml: str, relay: str) -> httpx.Response:
    return httpx.post(f"{issuer}/saml/acs", data={"SAMLResponse": saml, "RelayState": relay}, timeout=30,
                      follow_redirects=False)


def redeem(issuer: str, code: str, verifier: str, auth=("core", "core-secret")) -> httpx.Response:
    return httpx.post(f"{issuer}/token", auth=auth, timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier})


@pytest.mark.parametrize("sign_response", [False, True], ids=["assertion-signed", "response-signed"])
def test_saml_sign_in_becomes_an_oidc_id_token_with_the_users_groups(keycloak, cairn, sign_response):
    import jwt

    s, issuer = cairn
    realm(keycloak, s.saml.sp_entity_id, s.saml.acs_url, sign_response)
    # The service provider's metadata and the OpenID configuration Core reads.
    md = httpx.get(f"{issuer}/saml/metadata", timeout=10)
    assert md.status_code == 200 and s.saml.acs_url in md.text
    disc = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10).json()
    assert disc["issuer"] == issuer and disc["token_endpoint"] == f"{issuer}/token"

    got = sign_in(issuer)
    r = post_acs(issuer, got["saml"], got["relay"])
    assert r.status_code == 303, r.text
    back = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(REDIRECT) and back["state"] == [got["state"]]
    code = back["code"][0]
    tok = redeem(issuer, code, got["verifier"])
    assert tok.status_code == 200, tok.text
    keys = httpx.get(disc["jwks_uri"], timeout=10).json()
    key = jwt.PyJWK(keys["keys"][0]).key
    claims = jwt.decode(tok.json()["id_token"], key, algorithms=["RS256"], audience="core", issuer=issuer)
    assert claims["sub"] == "jane" and claims["email"] == "jane@acme.example" and claims["groups"] == ["finance"]
    assert claims["nonce"] == got["nonce"] and claims["amr"] == ["saml"]
    info = httpx.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=10)
    assert info.status_code == 200 and info.json()["groups"] == ["finance"]
    # An ID token is not a userinfo token, and a code works once.
    assert httpx.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok.json()['id_token']}"},
                     timeout=10).status_code == 401
    assert redeem(issuer, code, got["verifier"]).json() == {"error": "invalid_grant"}
    # The same signed response again: refused (its request is used, its assertion seen).
    again = post_acs(issuer, got["saml"], got["relay"])
    assert again.status_code == 403 and "refused" in again.text
    actions = [e["action"] for e in s.audit.entries("identity", limit=50)]
    assert "sso.saml_signed_in" in actions and "sso.saml_refused" in actions
    # The SSO key is not Trino's: an ID token can never pass as a Trino login.
    if getattr(s, "trino_tokens", None) is not None:
        assert s.trino_tokens.jwks()["keys"][0]["kid"] != keys["keys"][0]["kid"]


def test_tampered_forged_and_misdirected_responses_are_refused(keycloak, cairn):
    s, issuer = cairn
    realm(keycloak, s.saml.sp_entity_id, s.saml.acs_url, False)

    def tamper(saml: str, fn) -> str:
        return base64.b64encode(fn(base64.b64decode(saml))).decode()

    # The user changed after signing: the signature no longer verifies.
    got = sign_in(issuer)
    bad = tamper(got["saml"], lambda x: x.replace(b">jane<", b">admin<"))
    r = post_acs(issuer, bad, got["relay"])
    assert r.status_code == 403 and "signature" in r.text, r.text
    # A second assertion smuggled in beside the signed one (XML signature wrapping): refused.
    got = sign_in(issuer)
    raw = base64.b64decode(got["saml"])
    a = re.search(rb"<(\w+:)?Assertion[\s>].*?</(\w+:)?Assertion>", raw, re.S).group(0)
    evil = raw.replace(a, a.replace(b">jane<", b">admin<").replace(b'ID="', b'ID="x') + a, 1)
    r = post_acs(issuer, base64.b64encode(evil).decode(), got["relay"])
    assert r.status_code == 403 and "one assertion" in r.text, r.text
    # A genuine response for another sign-in (RelayState not the request it answers): refused.
    first, second = sign_in(issuer), sign_in(issuer)
    r = post_acs(issuer, first["saml"], second["relay"])
    assert r.status_code == 403 and "not for a sign-in Cairn started" in r.text
    # A DTD (entity expansion) is never parsed.
    dtd = base64.b64encode(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "a">]><x>&a;</x>').decode()
    assert post_acs(issuer, dtd, "x").status_code == 400
    # The second, untouched response still works once.
    r = post_acs(issuer, second["saml"], second["relay"])
    assert r.status_code == 303
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    assert redeem(issuer, code, "wrong-verifier").json() == {"error": "invalid_grant"}          # PKCE
    assert redeem(issuer, code, second["verifier"], ("core", "nope")).status_code == 401        # client secret


def test_oidc_clients_and_redirects_are_checked(keycloak, cairn):
    s, issuer = cairn
    realm(keycloak, s.saml.sp_entity_id, s.saml.acs_url, False)
    q = {"client_id": "core", "redirect_uri": REDIRECT, "response_type": "code", "scope": "openid"}
    assert httpx.get(f"{issuer}/authorize", params={**q, "client_id": "nobody"}).status_code == 400
    assert httpx.get(f"{issuer}/authorize", params={**q, "redirect_uri": "https://evil.example/cb"}).status_code == 400
    assert httpx.get(f"{issuer}/authorize", params={**q, "scope": "profile"}).status_code == 400
    assert httpx.get(f"{issuer}/authorize", params=q, follow_redirects=False).status_code == 302
