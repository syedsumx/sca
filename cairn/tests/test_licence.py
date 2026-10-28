"""Licence keys: verification, the state they put Cairn in, and what they allow."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.licence import (DEV_KEYS_PATH, LicenceError, LicenceRefused, LicenceService, issue, load_keys,
                                  verify)
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ROOT = Path(__file__).resolve().parent.parent
ADMIN, ANA, BOB, CAROL = (f"urn:li:corpuser:{n}" for n in ("admin", "ana", "bob", "carol"))
TODAY = date(2026, 11, 1)
VENDOR = Ed25519PrivateKey.generate()
KEYS = {"cairn-test": VENDOR.public_key()}


def lic(days: int = 365, start: date = TODAY, key=VENDOR, kid: str = "cairn-test", **extra) -> str:
    fields = {"id": "lic_1", "customer": "Acme", "edition": "enterprise", "issued_at": start.isoformat(),
              "expires_at": (start + timedelta(days=days)).isoformat(), **extra}
    return issue(key, kid, **fields)


def service(engine, today=TODAY, **kw) -> LicenceService:
    return LicenceService(engine, keys=KEYS, dev_keys={}, today=lambda: today, **kw)


def test_verify_accepts_only_untampered_licences_from_known_keys():
    good = verify(lic(max_users=10, tenant="acme"), KEYS, {}, tenant="acme")
    assert good.customer == "Acme" and good.max_users == 10 and "ai" in good.allowed and not good.development
    prefix, payload, sig = lic().split(".")
    doc = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged = base64.urlsafe_b64encode(json.dumps({**doc, "max_users": 10_000}).encode()).rstrip(b"=").decode()
    cases = {f"{prefix}.{forged}.{sig}": "signature does not match",
             lic(key=Ed25519PrivateKey.generate()): "signature does not match",
             lic(kid="someone-else"): "unknown key",
             "not-a-key": "not a Cairn licence",
             lic(edition="platinum"): "unknown edition",
             lic(features=["teleport"]): "unknown features"}
    for token, why in cases.items():
        with pytest.raises(LicenceError, match=why):
            verify(token, KEYS, {})
    with pytest.raises(LicenceError, match="for tenant 'acme', this deployment is 'globex'"):
        verify(lic(tenant="acme"), KEYS, {}, tenant="globex")


def test_development_key_is_capped():
    dev = serialization.load_pem_private_key((ROOT / "deploy/licence/dev-private-key.pem").read_bytes(), None)
    dev_keys = load_keys(DEV_KEYS_PATH)
    assert set(dev_keys) == {"cairn-dev"}
    got = verify(lic(days=30, key=dev, kid="cairn-dev", max_users=500), {}, dev_keys)
    assert got.development and got.max_users == 5
    with pytest.raises(LicenceError, match="at most 31 days"):
        verify(lic(days=365, key=dev, kid="cairn-dev"), {}, dev_keys)
    assert load_keys(ROOT / "deploy/licence/licence-keys.json") == {}       # no production key in the tree


def test_state_follows_expiry_and_grace(engine):
    assert service(engine).state()["status"] == "evaluation"
    missing = service(engine, required=True).state()
    assert (missing["status"], missing["read_only"]) == ("missing", True)
    svc = service(engine)
    svc.install(lic(days=30, grace_days=7), ADMIN)
    for day, status, read_only in ((TODAY, "valid", False), (TODAY + timedelta(days=33), "expiring", False),
                                   (TODAY + timedelta(days=38), "expired", True)):
        st = service(engine, today=day).state()
        assert (st["status"], st["read_only"]) == (status, read_only), day


def test_enforcement(engine):
    svc = service(engine)
    svc.check(ANA, False, "POST", "/ai/ask")                                # evaluation: everything
    svc.install(lic(edition="standard", features=["automations"], max_users=2), ADMIN)
    with pytest.raises(LicenceRefused, match="AI search.*not part of the standard edition"):
        svc.check(ANA, False, "GET", "/ai/search")
    svc.check(ANA, False, "POST", "/governance/automations")                # an extra feature
    svc.check(ANA, False, "POST", "/collab/threads")
    svc.check(BOB, False, "PUT", "/governance/forms")
    svc.check(CAROL, False, "GET", "/collab/threads")                       # third person: reads only
    with pytest.raises(LicenceRefused, match="all 2 seats are taken"):
        svc.check(CAROL, False, "POST", "/collab/threads")
    svc.check(CAROL, False, "POST", "/collab/enrich")                       # a POST that only reads
    svc.check(ANA, False, "POST", "/collab/threads")                        # seated people keep writing
    svc.check("urn:li:corpuser:cairn-service", True, "POST", "/ai/ask")     # services are never limited

    expired = service(engine, today=TODAY + timedelta(days=400))
    with pytest.raises(LicenceRefused, match="expired: Cairn is read-only"):
        expired.check(ANA, False, "DELETE", "/collab/threads/1")
    expired.check(ANA, False, "GET", "/collab/threads")
    expired.check(ANA, False, "PUT", "/admin/licence")                      # a new licence can be installed
    expired.check(ANA, False, "POST", "/scim/v2/Users")                     # and people deprovisioned

    svc.remove(ADMIN)
    with pytest.raises(LicenceRefused, match="no licence installed"):
        service(engine, required=True).check(ANA, False, "POST", "/collab/threads")


def test_api_install_and_state(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.licence = service(s.engine, required=True, tenant="acme")
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    c = TestClient(create_api_app(s))
    admin, ana = {"X-Cairn-Actor": ADMIN}, {"X-Cairn-Actor": ANA}
    assert c.get("/admin/licence", headers=ana).json() == {"status": "missing", "read_only": True,
                                                           "days_left": None, "over_seats": False}
    assert c.post("/collab/threads", headers=ana, json={"entity_urn": "urn:li:dataset:x", "body": "hi"}).status_code == 402
    assert c.put("/admin/licence", headers=ana, json={"key": lic(tenant="acme")}).status_code == 403
    bad = c.put("/admin/licence", headers=admin, json={"key": lic(tenant="globex")})
    assert bad.status_code == 422 and "tenant 'globex'" in bad.json()["detail"]
    st = c.put("/admin/licence", headers=admin, json={"key": lic(tenant="acme", max_users=50)}).json()
    assert st["status"] == "valid" and st["licence"]["customer"] == "Acme" and st["seats_limit"] == 50
    assert c.post("/collab/threads", headers=ana, json={"entity_urn": "urn:li:dataset:x", "body": "hi"}).status_code != 402
    assert "licence" not in c.get("/admin/licence", headers=ana).json()        # details are for admins
    assert c.delete("/admin/licence", headers=admin).json()["status"] == "missing"


def test_vendor_tool_issues_licences_the_cli_verifies(tmp_path):
    key = tmp_path / "k.pem"
    out = subprocess.run([sys.executable, str(ROOT / "deploy/ops/licence.py"), "keygen", "--kid", "t", "--out",
                          str(key)], capture_output=True, text=True, check=True)
    pub = json.loads(out.stdout.splitlines()[0])
    token = subprocess.run([sys.executable, str(ROOT / "deploy/ops/licence.py"), "issue", "--key", str(key), "--kid",
                            "t", "--customer", "Globex", "--edition", "standard", "--feature", "ai", "--max-users",
                            "9", "--days", "30"], capture_output=True, text=True, check=True).stdout.strip()
    keys = {"t": serialization.load_pem_public_key(serialization.load_pem_private_key(key.read_bytes(), None)
                                                   .public_key().public_bytes(serialization.Encoding.PEM,
                                                                              serialization.PublicFormat.SubjectPublicKeyInfo))}
    got = verify(token, keys, {})
    assert got.customer == "Globex" and got.allowed == {"ai"} and got.max_users == 9 and "t" in pub
    again = subprocess.run([sys.executable, str(ROOT / "deploy/ops/licence.py"), "keygen", "--kid", "t", "--out",
                            str(key)], capture_output=True, text=True)
    assert again.returncode != 0 and "refusing to overwrite" in again.stderr


def test_licence_file_is_installed_at_start(engine, tmp_path, monkeypatch):
    import cairn.common.licence as mod

    path = tmp_path / "licence"
    path.write_text(lic(customer="Hosted") + "\n")
    monkeypatch.setenv("CAIRN_LICENCE_FILE", str(path))
    monkeypatch.setattr(mod, "load_keys", lambda p=None: KEYS if p is None or p == mod.KEYS_PATH else {})
    assert mod.from_env(engine).current()[0].customer == "Hosted"
    assert mod.from_env(engine).current()[0].customer == "Hosted"          # unchanged file: left alone
    path.write_text("garbage")
    assert mod.from_env(engine).current()[0].customer == "Hosted"          # a bad file never removes a licence
