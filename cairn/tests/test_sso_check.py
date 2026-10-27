"""`cairn sso check` against the test identity provider (tests/e2e/oidc_provider.py), in-process."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from cairn.common import sso


@pytest.fixture(scope="module")
def idp():
    srv_holder = {}
    saved = {k: os.environ.get(k) for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_USERS", "OIDC_ISSUER")}
    os.environ.update(OIDC_CLIENT_ID="cairn", OIDC_CLIENT_SECRET="s3cret", OIDC_USERS=json.dumps({"dana": {}}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    os.environ["OIDC_ISSUER"] = f"http://127.0.0.1:{srv.server_port}"
    spec = importlib.util.spec_from_file_location(
        "oidc_provider", os.path.join(os.path.dirname(__file__), "e2e", "oidc_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv.RequestHandlerClass = mod.Handler
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv_holder["url"] = os.environ["OIDC_ISSUER"]
    yield srv_holder["url"]
    srv.shutdown()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _run(idp, **kw):
    args = {"discovery_url": f"{idp}/.well-known/openid-configuration", "client_id": "cairn",
            "client_secret": "s3cret", "base_url": "http://localhost:9000"}
    args.update(kw)
    return sso.check(**args)


def test_good_settings_pass(idp):
    report = _run(idp)
    assert report.ok, report.as_dict()
    assert report.callback_url == "http://localhost:9000/callback/oidc"
    names = {c.name for c in report.checks}
    assert {"discovery", "signing keys", "client credentials", "PKCE"} <= names


def test_wrong_secret_is_reported(idp):
    report = _run(idp, client_secret="nope")
    assert not report.ok
    assert [c.detail for c in report.checks if c.name == "client credentials"] == ["rejected (invalid_client)"]


def test_post_auth_method(idp):
    assert _run(idp, auth_method="client_secret_post").ok


def test_unreachable_discovery_and_missing_settings():
    assert not sso.check("http://127.0.0.1:9/.well-known/openid-configuration", "c", "s", "http://localhost").ok
    report = sso.check("", "c", "", "http://localhost")
    assert not report.ok and "CAIRN_SSO_DISCOVERY_URL" in report.checks[0].detail


def test_plain_http_public_url_is_only_a_warning(idp):
    report = _run(idp, base_url="http://cairn.internal")
    assert report.ok
    assert any(c.warning and c.name == "public URL" for c in report.checks)
