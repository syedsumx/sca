"""Every API route, called without credentials, is refused, unless it is on the short list of routes that
are public by design (each with why). A new route that answers strangers fails this test until it is
added here on purpose. Answers carry headers that keep them out of caches and sniffers."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.config import Settings
from cairn.services import build_services

# (method, path): why it answers without a Cairn token, and what it answers a stranger.
PUBLIC = {
    ("POST", "/oauth/token"): "OAuth token endpoint: the client authenticates in the body (400 without it)",
    ("POST", "/oauth/revoke"): "RFC 7009: revocation answers 200 whatever the token",
    ("POST", "/oauth/register"): "RFC 7591 registration: off, admin-approved or open by setting; public clients only",
    ("POST", "/v1/lakehouse/oauth/tokens"): "engines exchange client credentials for catalog tokens (400 without)",
    ("POST", "/v1/shares/oauth/tokens"): "share recipients exchange their credential (400 without)",
    ("GET", "/governance/privacy/proof-keys"): "the public key erasure proofs are signed with: auditors check them without Cairn",
    ("POST", "/governance/privacy/proof/verify"): "checks a signed erasure proof someone was given (it holds no identifier)",
    ("GET", "/embed/v1/sdk.js"): "the embed SDK script (404 until embeds are configured)",
    ("GET", "/embed/v1/assets/{urn}"): "embeds authenticate with a signed embed token (422 without)",
    ("GET", "/embed/v1/frame/{kind}"): "as above",
    ("GET", "/embed/v1/lineage/{urn}"): "as above",
    ("GET", "/embed/v1/quality/{urn}"): "as above",
    ("POST", "/integrations/git/{rid}/webhook"): "webhooks verify the host's signature (404 when not configured)",
    ("POST", "/integrations/slack/commands"): "Slack signs its requests (404 when not configured)",
    ("POST", "/integrations/slack/events"): "as above",
    ("POST", "/integrations/slack/interactions"): "as above",
    ("POST", "/integrations/teams/messages"): "Teams signs its requests (404 when not configured)",
    ("POST", "/notify/inbound/{system_id}"): "inbound notifications carry their system's secret (404 when unknown)",
}
SCIM = re.compile(r"^/scim/v2/")        # SCIM needs its own bearer token: 503 when SCIM is not set up, 401 when it is


@pytest.fixture
def app(tmp_path, monkeypatch, graph):
    for k, v in {"CAIRN_PIPES_STORE": json.dumps({"endpoint": "http://127.0.0.1:1"}),
                 "CAIRN_PIPES_CATALOG": json.dumps({"uri": "http://127.0.0.1:1"}),
                 "CAIRN_STREAMS_ENABLED": "true", "CAIRN_MAINTENANCE_CONNECTION": "lakehouse",
                 "CAIRN_TRINO_ACCESS_KEY": "k" * 32, "CAIRN_TRINO_CATALOGS": '{"lakehouse": {"platform": "iceberg"}}'}.items():
        monkeypatch.setenv(k, v)
    # Production settings: no trusted actor header.
    s = build_services(Settings(database_url=f"sqlite:///{tmp_path}/c.db", service_token="svc-secret",
                                gateway={"key_dir": str(tmp_path / "keys")}), graph=graph, with_ai=False, with_gateway=False)
    return create_api_app(s)


def test_strangers_are_refused_everywhere_but_the_public_routes(app):
    c = TestClient(app, raise_server_exceptions=False)
    schema = app.openapi()["paths"]
    assert len(schema) > 250                                          # the whole API, not a corner of it
    unexpected, seen_public = [], set()
    for template, ops in schema.items():
        path = re.sub(r"\{[^}]+\}", "x", template)
        for method in ops:
            method = method.upper()
            r = c.request(method, path, json={} if method in ("POST", "PUT", "PATCH") else None,
                          headers={"X-Cairn-Actor": "urn:li:corpuser:admin"})        # not trusted in production
            if SCIM.match(template) and r.status_code == 503:
                continue
            assert r.status_code < 500, (method, template, r.status_code, r.text[:200])     # no crash, no trace
            if (method, template) in PUBLIC:
                seen_public.add((method, template))
                continue
            if r.status_code not in (401, 403):
                unexpected.append((method, template, r.status_code))
    assert unexpected == [], "routes that answer without credentials: add them to PUBLIC with why, or protect them"
    assert seen_public == set(PUBLIC), set(PUBLIC) - seen_public      # the list keeps up with the API


def test_answers_are_not_cached_or_sniffed(app):
    c = TestClient(app, raise_server_exceptions=False)
    for r in (c.get("/health"), c.get("/v1/pipes"), c.get("/me")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
