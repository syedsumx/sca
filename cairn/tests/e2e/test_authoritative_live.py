"""Authoritative mode (2.5) against the real Cairn Core: with ``CAIRN_NATIVE_POLICIES_ENABLED=false`` the Cairn
authorizer is Core's only authorizer and the policy decision point alone decides.

Checked as a person with their own token, through Cairn Core's GraphQL (the UI's path, through the masking proxy):
- without any grant, the PDP says DENY (default deny, not "not applicable") and Core refuses;
- a Cairn allow alone grants what no native policy grants;
- a native Core policy granting the same does not hold against a Cairn deny (deny overrides), and counts only once
  the PDP has imported it;
- with the PDP down, Core refuses the person (fail closed) while the break-glass admin still gets in, and access
  returns with the PDP.
(``test_14`` in test_active_metadata_live.py checks the same deny on viewing a dataset that native policies allow.)

Runs only against a stack in authoritative mode: the CI job ``stack`` switches the stack to it after the additive
suite (CAIRN_AUTHZ_MODE=authoritative, CAIRN_NATIVE_POLICIES_ENABLED=false) and runs ``tests/e2e/run.sh -k
authoritative``."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from test_active_metadata_live import client, compose, frontend_gql, mint_token
from test_stack_smoke import API, TOKEN, emit, eventually, gql

MODE = os.environ.get("CAIRN_E2E_AUTHZ_MODE", "additive")
pytestmark = [
    pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack"),
    pytest.mark.skipif(MODE != "authoritative", reason="the stack runs in additive mode (CAIRN_AUTHZ_MODE)"),
]

RUN = uuid.uuid4().hex[:8]
BOB = f"urn:li:corpuser:e2e_auth_bob_{RUN}"
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,auth_{RUN}.public.accounts,PROD)"
SERVICE_TOKEN = os.environ.get("CAIRN_E2E_PDP_TOKEN", "")
TAG_PRIVILEGES = ["CREATE_TAGS", "MANAGE_TAGS"]
CREATE_TAG = "mutation($i: CreateTagInput!) { createTag(input: $i) }"
VIEW = "query($u: String!) { dataset(urn: $u) { urn properties { name description } } }"
DESCRIPTION = "E2E accounts (authoritative mode)."


def decision(actor: str, privilege: str, urn: str | None = None) -> dict:
    body = {"actor": actor, "privilege": privilege}
    if urn:
        body["resource"] = {"type": "dataset", "urn": urn}
    r = httpx.post(f"{API}/governance/v1/authorize", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                   json=body, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def creates_tag(token: str) -> bool:
    name = f"e2e-auth-{uuid.uuid4().hex[:8]}"
    body = frontend_gql(token, CREATE_TAG, {"i": {"id": name, "name": name}}).json()
    return bool((body.get("data") or {}).get("createTag")) and not body.get("errors")


def views(token: str) -> bool:
    """Core answers a refused view with a *restricted* entity (its urn and name kept, every other field blanked:
    upstream ``AuthorizationUtils.restrictEntity``), so the description, not the name, says the view was allowed."""
    body = frontend_gql(token, VIEW, {"u": DS}).json()
    props = ((body.get("data") or {}).get("dataset") or {}).get("properties") or {}
    return props.get("description") == DESCRIPTION and not body.get("errors")


@pytest.fixture(scope="module")
def bob():
    emit(BOB, "corpUserInfo", {"active": True, "displayName": "Bob Authoritative", "email": f"bob-{RUN}@example.com"})
    emit(DS, "datasetProperties", {"name": "accounts", "description": DESCRIPTION})
    return mint_token(BOB)


@pytest.fixture
def policies():
    """Cairn's policy bundle, restored after the test."""
    with client(TOKEN) as root:
        saved = root.get("/governance/policies").json()["policies"]

        def put(*extra):
            r = root.put("/governance/policies", json={"policies": saved + list(extra)})
            assert r.status_code == 200, r.text
        yield put
        root.put("/governance/policies", json={"policies": saved})


def _rule(pid: str, effect: str, actions: list[str]) -> dict:
    return {"id": f"{pid}-{RUN}", "description": f"e2e authoritative: {pid}", "effect": effect, "priority": 500,
            "actions": actions, "condition": {"attr": "subject.urn", "op": "eq", "value": BOB}}


def test_authoritative_without_a_grant_the_pdp_denies_and_core_refuses(bob):
    d = decision(BOB, "CREATE_TAGS")
    assert d["decision"] == "DENY" and "default deny" in d["reason"], d       # additive mode says NOT_APPLICABLE
    assert not creates_tag(bob)


def test_authoritative_a_cairn_allow_alone_grants(bob, policies):
    policies(_rule("allow-tags", "allow", TAG_PRIVILEGES))
    eventually(lambda: decision(BOB, "CREATE_TAGS")["decision"] == "ALLOW", timeout=60)
    eventually(lambda: creates_tag(bob), timeout=90, interval=5)             # past Core's decision cache


def test_authoritative_a_native_grant_does_not_hold_against_a_cairn_deny(bob, policies):
    urn = gql("""mutation($input: PolicyUpdateInput!) { createPolicy(input: $input) }""", {"input": {
        "type": "PLATFORM", "name": f"e2e native tags {RUN}", "state": "ACTIVE",
        "description": "e2e: a native grant the PDP must overrule", "privileges": TAG_PRIVILEGES,
        "actors": {"users": [BOB], "groups": [], "resourceOwners": False, "allUsers": False, "allGroups": False}}})["createPolicy"]
    try:
        policies(_rule("deny-tags", "deny", TAG_PRIVILEGES))
        eventually(lambda: decision(BOB, "CREATE_TAGS")["decision"] == "DENY", timeout=60)
        eventually(lambda: not creates_tag(bob), timeout=90, interval=5)
        assert not creates_tag(bob)                                          # the native policy is there, and active
        policies()                                                           # the deny gone: the native grant, imported
        d = eventually(lambda: (lambda x: x if x["decision"] == "ALLOW" else None)(decision(BOB, "CREATE_TAGS")),
                       timeout=300, interval=5)
        assert not any(p.endswith(RUN) for p in d.get("policy_ids", [])), d  # granted by the imported native policy
        eventually(lambda: creates_tag(bob), timeout=90, interval=5)
    finally:
        gql("mutation($u: String!) { deletePolicy(urn: $u) }", {"u": urn})


def test_authoritative_with_the_pdp_down_core_fails_closed(bob):
    eventually(lambda: views(bob), timeout=120, interval=5)                  # viewing is granted (native, imported)
    compose("stop", "cairn-pdp")
    try:
        try:
            eventually(lambda: not views(bob), timeout=120, interval=5)      # past Core's decision cache: refused
        except AssertionError as e:
            # Which part let the view through: what bob got, and what Core's authorizer and the proxy said.
            body = frontend_gql(bob, VIEW, {"u": DS}).text[:1500]
            core = [ln for ln in compose("logs", "--no-color", "--tail", "3000", "cairn-core").splitlines()
                    if any(k in ln for k in ("Cairn PDP call failed", "fail-closed", "fail-open", "circuit",
                                             "CairnAuthorizer", "Authoriz"))]
            proxy = [ln for ln in compose("logs", "--no-color", "--tail", "2000", "cairn-core-proxy").splitlines()
                     if "decision" in ln or "authoritative" in ln]
            ps = compose("ps", "--all", "cairn-pdp")
            raise AssertionError(f"{e}\nbob got: {body}\npdp: {ps}\ncore authorizer:\n" + "\n".join(core[-40:])
                                 + "\nproxy:\n" + "\n".join(proxy[-20:])) from None
        assert views(TOKEN)                                                  # the break-glass admin still gets in
    finally:
        compose("up", "-d", "--no-build", "--wait", "cairn-pdp", timeout=600)
    eventually(lambda: views(bob), timeout=180, interval=5)                  # the circuit closes again
