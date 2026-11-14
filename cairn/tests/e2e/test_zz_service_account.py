"""Live checks of the control-plane service account (runs last: it rotates the token).

The control plane must act as ``urn:li:corpuser:cairn-service`` with a least-privilege grant, and the
token keeper must rotate that token without breaking the services.
"""

from __future__ import annotations

import os
import subprocess
import time

import httpx
import pytest

from test_stack_smoke import API, COMPOSE, CORE, TOKEN, eventually

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

SERVICE = "urn:li:corpuser:cairn-service"
ROOT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")


def gql_as(token: str, query: str, variables: dict | None = None) -> dict:
    r = httpx.post(f"{CORE}/api/graphql", json={"query": query, "variables": variables or {}},
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.json() if r.status_code == 200 else {"errors": [{"message": f"HTTP {r.status_code}"}]}


def compose(*args: str) -> str:
    out = subprocess.run(COMPOSE + list(args), capture_output=True, text=True, timeout=300, cwd=ROOT_DIR)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def service_token() -> str:
    return compose("exec", "-T", "cairn-api", "cat", "/run/cairn/core_token").strip()


def unauthorized(resp: dict) -> bool:
    text = str(resp.get("errors", "")).lower()
    return bool(resp.get("errors")) and any(w in text for w in ("unauthorized", "forbidden", "not authorized", "403"))


def test_control_plane_runs_as_service_account():
    me = gql_as(service_token(), "{ me { corpUser { urn } platformPrivileges { managePolicies manageIdentities "
                                 "manageTokens manageIngestion manageGlossaries } } }")["data"]["me"]
    assert me["corpUser"]["urn"] == SERVICE
    privs = me["platformPrivileges"]
    assert privs["manageIngestion"] and privs["manageGlossaries"]
    assert not privs["managePolicies"] and not privs["manageIdentities"] and not privs["manageTokens"]


def test_service_account_cannot_escalate():
    tok = service_token()
    # cannot write policies, mint tokens for others, or create users
    assert unauthorized(gql_as(tok, """mutation { createPolicy(input: { type: PLATFORM, name: "escalate",
        state: ACTIVE, privileges: ["MANAGE_POLICIES"], actors: { users: ["%s"], allUsers: false,
        allGroups: false, resourceOwners: false } }) }""" % SERVICE))
    assert unauthorized(gql_as(tok, """mutation { createAccessToken(input: { type: PERSONAL,
        actorUrn: "urn:li:corpuser:admin", duration: ONE_HOUR, name: "steal" }) { accessToken } }"""))
    # cannot delete entities
    r = httpx.delete(f"{CORE}/openapi/v3/entity/tag/{'urn%3Ali%3Atag%3Agdpr-subject'}",
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    assert r.status_code in (401, 403), r.status_code


def test_token_rotation_keeps_services_working():
    old = service_token()
    # Rotate now, then revoke the retired token immediately (grace 0) on the next check.
    compose("run", "--rm", "--no-deps", "cairn-token-keeper", "python", "/opt/cairn-bootstrap/mint_core_token.py",
            "--out", "/run/cairn/core_token", "--keep", "--once", "--rotate-days", "0", "--grace-minutes", "0")
    new = service_token()
    assert new and new != old
    compose("run", "--rm", "--no-deps", "cairn-token-keeper", "python", "/opt/cairn-bootstrap/mint_core_token.py",
            "--out", "/run/cairn/core_token", "--keep", "--once", "--rotate-days", "20", "--grace-minutes", "0")
    assert gql_as(old, "{ me { corpUser { urn } } }").get("errors"), "retired token must be revoked"
    assert gql_as(new, "{ me { corpUser { urn } } }")["data"]["me"]["corpUser"]["urn"] == SERVICE

    # Services pick up the new token without a restart: a call that needs Cairn Core succeeds.
    def search_ok():
        r = httpx.get(f"{API}/marketplace/products", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        return r.status_code == 200
    eventually(search_ok, timeout=60)

    # Pipelines restart on rotation and come back healthy.
    def pipelines_up():
        ps = compose("ps", "--format", "{{.Service}} {{.State}}", "cairn-actions-pii", "cairn-actions-labels")
        return ps.count("running") == 2
    time.sleep(40)
    eventually(pipelines_up, timeout=120)
