"""Guided single sign-on setup: ``cairn sso check`` validates OIDC settings against the provider.

Checks, in order: the discovery document (issuer, endpoints, signing algorithm, code flow, PKCE),
the signing keys, and the client credentials. Credentials are tested with a token request for a
made-up authorization code: providers answer ``invalid_client`` for bad credentials and
``invalid_grant`` for good ones, so nothing is signed in and no user is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

CALLBACK_PATH = "/callback/oidc"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False   # informational: does not fail the check


@dataclass
class Report:
    callback_url: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok or c.warning for c in self.checks)

    def add(self, name: str, ok: bool, detail: str, warning: bool = False) -> bool:
        self.checks.append(Check(name, ok, detail, warning and not ok))
        return ok

    def as_dict(self) -> dict:
        return {"ok": self.ok, "callback_url": self.callback_url,
                "checks": [{"check": c.name, "ok": c.ok, "detail": c.detail, **({"warning": True} if c.warning else {})}
                           for c in self.checks]}


def check(discovery_url: str, client_id: str, client_secret: str, base_url: str,
          auth_method: str = "client_secret_basic", client: Optional[httpx.Client] = None) -> Report:
    http = client or httpx.Client(timeout=15, follow_redirects=True)
    report = Report(callback_url=base_url.rstrip("/") + CALLBACK_PATH)
    missing = [n for n, v in (("CAIRN_SSO_DISCOVERY_URL", discovery_url), ("CAIRN_SSO_CLIENT_ID", client_id),
                              ("CAIRN_SSO_CLIENT_SECRET", client_secret), ("CAIRN_PUBLIC_URL", base_url)) if not v]
    if not report.add("settings", not missing, "missing: " + ", ".join(missing) if missing else "all set"):
        return report
    report.add("public URL", base_url.startswith("https://") or "localhost" in base_url or "127.0.0.1" in base_url,
               base_url if base_url.startswith("https://") else f"{base_url}: providers require https outside localhost",
               warning=True)

    try:
        resp = http.get(discovery_url)
        doc = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        doc, resp = None, None
        report.add("discovery", False, f"{discovery_url}: {e}")
        return report
    if not report.add("discovery", isinstance(doc, dict),
                      f"{discovery_url}: HTTP {resp.status_code}" if doc is None else f"issuer {doc.get('issuer')}"):
        return report

    need = ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"]
    absent = [k for k in need if not doc.get(k)]
    report.add("endpoints", not absent, "missing: " + ", ".join(absent) if absent else "authorization, token, keys")
    expected_issuer = discovery_url.split("/.well-known/")[0].rstrip("/")
    report.add("issuer", (doc.get("issuer") or "").rstrip("/") == expected_issuer,
               f"{doc.get('issuer')} (discovery URL is under {expected_issuer})", warning=True)
    algs = doc.get("id_token_signing_alg_values_supported") or []
    report.add("signing", "RS256" in algs or not algs, f"ID token algorithms: {', '.join(algs) or 'not listed'}")
    flows = doc.get("response_types_supported") or ["code"]
    report.add("code flow", "code" in flows, f"response types: {', '.join(flows)}")
    pkce = doc.get("code_challenge_methods_supported") or []
    report.add("PKCE", "S256" in pkce, "S256 supported" if "S256" in pkce else
               "S256 not advertised: set AUTH_OIDC_DISABLE_PKCE=true if sign-in fails", warning=True)
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    report.add("client authentication", auth_method in methods, f"{auth_method} (provider: {', '.join(methods)})")

    if doc.get("jwks_uri"):
        try:
            keys = http.get(doc["jwks_uri"]).json().get("keys") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            keys = []
        report.add("signing keys", bool(keys), f"{len(keys)} key(s)" if keys else f"none at {doc['jwks_uri']}")

    if doc.get("token_endpoint"):
        form = {"grant_type": "authorization_code", "code": "cairn-sso-check", "redirect_uri": report.callback_url}
        kwargs: dict = {"data": form}
        if auth_method == "client_secret_post":
            form.update(client_id=client_id, client_secret=client_secret)
        else:
            kwargs["auth"] = (client_id, client_secret)
        try:
            r = http.post(doc["token_endpoint"], **kwargs)
            error = (r.json() if "json" in r.headers.get("content-type", "") else {}).get("error", f"HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        report.add("client credentials", error != "invalid_client" and error in ("invalid_grant", "invalid_request"),
                   "accepted" if error in ("invalid_grant", "invalid_request") else f"rejected ({error})")
    return report
