"""Single sign-on with OpenID Connect, against the test identity provider (docker-compose.sso.yml).

The guided setup check passes against the provider, a person signs in through the login page's SSO
button, is provisioned with the identity provider's groups, and those groups drive Cairn policy
decisions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid

import httpx
import pytest

from test_stack_smoke import API, COMPOSE, EDGE, TOKEN, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E_SSO") != "1", reason="SSO stack not enabled")

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
USER = "urn:li:corpuser:dana.sso"
GROUP = "urn:li:corpGroup:sso-analysts"
LEAK = re.compile(r"data\s?hub|acryl|atlan(?![a-z])", re.I)


def test_guided_setup_check_passes_and_catches_a_wrong_secret():
    env = {"CAIRN_SSO_DISCOVERY_URL": "http://cairn-oidc:8891/.well-known/openid-configuration",
           "CAIRN_SSO_CLIENT_ID": "cairn-e2e", "CAIRN_PUBLIC_URL": "http://localhost:9000"}

    def run(secret: str) -> tuple[int, dict]:
        flags = [x for k, v in {**env, "CAIRN_SSO_CLIENT_SECRET": secret}.items() for x in ("-e", f"{k}={v}")]
        out = subprocess.run(COMPOSE + ["exec", "-T", *flags, "cairn-api", "cairn", "sso", "check", "--json"],
                             capture_output=True, text=True, timeout=120, cwd=ROOT_DIR)
        return out.returncode, json.loads(out.stdout)

    code, report = run(os.environ["CAIRN_E2E_SSO_SECRET"])
    assert code == 0 and report["ok"], report
    assert report["callback_url"] == "http://localhost:9000/callback/oidc"
    code, report = run("wrong")
    assert code == 1
    assert [c["detail"] for c in report["checks"] if c["check"] == "client credentials"] == ["rejected (invalid_client)"]


def test_sign_in_with_sso_provisions_the_user_and_groups():
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_sso.js")], capture_output=True,
                         text=True, timeout=240, env={**os.environ, "EDGE": EDGE, "SSO_USER": "dana.sso",
                                                      "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", "")})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["ssoButton"] and result["signedIn"], result
    assert not LEAK.search(result["loginText"]), result["loginText"]
    assert result["me"]["urn"] == USER
    assert result["me"]["properties"]["email"] == "dana.sso@example.com"

    def groups():
        data = gql("""query($u: String!) { corpUser(urn: $u) { relationships(input: {
            types: ["IsMemberOfGroup", "IsMemberOfNativeGroup"], direction: OUTGOING, start: 0, count: 20 }) {
            relationships { entity { urn } } } } }""", {"u": USER})
        return {r["entity"]["urn"] for r in data["corpUser"]["relationships"]["relationships"]}
    assert GROUP in eventually(lambda: GROUP in groups() and groups(), timeout=60)


def test_identity_provider_groups_drive_policy_decisions():
    probe = f"CAIRN_E2E_SSO_PROBE_{uuid.uuid4().hex[:6].upper()}"
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as root:
        saved = root.get("/governance/policies").json()["policies"]
        rule = {"id": f"e2e-sso-{probe.lower()[-6:]}", "description": "e2e: SSO group grant", "effect": "allow",
                "actions": [probe], "condition": {"attr": "subject.groups", "op": "contains", "value": GROUP}}
        assert root.put("/governance/policies", json={"policies": saved + [rule]}).status_code == 200
        try:
            def decision(actor: str) -> str:
                r = httpx.post(f"{API}/governance/v1/authorize", timeout=30,
                               headers={"Authorization": f"Bearer {os.environ['CAIRN_E2E_PDP_TOKEN']}"},
                               json={"actor": actor, "privilege": probe, "resource": None})
                assert r.status_code == 200, r.text
                return r.json()["decision"]
            assert eventually(lambda: decision(USER) == "ALLOW", timeout=60)
            assert decision("urn:li:corpuser:admin-not-in-group") != "ALLOW"
        finally:
            root.put("/governance/policies", json={"policies": saved})
