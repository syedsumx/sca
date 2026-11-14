"""Hosted signup and Stripe billing, end to end through the Cairn API: stripe-mock (Stripe's own API mock) answers
the Stripe calls, Mailpit is the SMTP server the confirmation, welcome and billing emails go through, Stripe's
webhooks are signed as Stripe signs them, and provisioning runs a command (here a stand-in for
deploy/hosting/tenant.sh) that checks the licence it is given against the signing key. Needs docker."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

STRIPE_IMAGE = os.environ.get("CAIRN_STRIPE_MOCK_IMAGE", "stripe/stripe-mock:v0.194.0")
MAIL_IMAGE = os.environ.get("CAIRN_MAILPIT_IMAGE", "axllent/mailpit:v1.21.8")
WEBHOOK_SECRET = "whsec_test_8f2c1d"


def _images() -> bool:
    return shutil.which("docker") is not None and all(
        subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode == 0 for i in (STRIPE_IMAGE, MAIL_IMAGE))


pytestmark = pytest.mark.skipif(not _images(), reason=f"needs docker, {STRIPE_IMAGE} and {MAIL_IMAGE}")


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(p: int) -> None:
    end = time.time() + 60
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), 1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"port {p} did not open")


def ready(url: str, ok, headers=None, seconds: int = 60) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if ok(httpx.get(url, headers=headers or {}, timeout=3)):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not answer")


def ready_smtp(p: int, seconds: int = 60) -> None:
    import smtplib

    end = time.time() + seconds
    while time.time() < end:
        try:
            with smtplib.SMTP("127.0.0.1", p, timeout=3) as s:
                if s.noop()[0] == 250:
                    return
        except (OSError, smtplib.SMTPException):
            pass
        time.sleep(0.5)
    raise RuntimeError("the SMTP server did not answer")


@pytest.fixture(scope="module")
def services():
    names = [f"cairn-stripe-{os.getpid()}", f"cairn-mail-{os.getpid()}"]
    sp, smtp, mapi = port(), port(), port()
    subprocess.run(["docker", "run", "-d", "--name", names[0], "-p", f"127.0.0.1:{sp}:12111", STRIPE_IMAGE],
                   check=True, capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", names[1], "-p", f"127.0.0.1:{smtp}:1025", "-p", f"127.0.0.1:{mapi}:8025",
                    MAIL_IMAGE], check=True, capture_output=True)
    for p in (sp, smtp, mapi):
        wait(p)
    # Docker's port proxy accepts before the servers inside are up: wait until each one answers.
    ready(f"http://127.0.0.1:{mapi}/api/v1/messages", lambda r: r.status_code == 200)
    ready(f"http://127.0.0.1:{sp}/v1/customers", lambda r: r.status_code in (200, 401),
          headers={"Authorization": "Bearer sk_test_123"})
    ready_smtp(smtp)
    yield {"stripe": f"http://127.0.0.1:{sp}", "smtp": smtp, "mail": f"http://127.0.0.1:{mapi}"}
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


PROVISION = r'''
import base64, json, os, sys
from cryptography.hazmat.primitives import serialization
action, tenant, flag, path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
log, pub = os.environ["PROVISION_LOG"], os.environ["LICENCE_PUB"]
if os.path.exists(os.environ["PROVISION_FAIL"]):
    print("kubernetes is unreachable", file=sys.stderr); sys.exit(1)
assert flag == "--licence" and oct(os.stat(path).st_mode & 0o777) == "0o600", (flag, oct(os.stat(path).st_mode))
text = open(path).read()
_, payload, sig = text.split(".")
pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
key = serialization.load_pem_public_key(open(pub, "rb").read())
key.verify(pad(sig), pad(payload))          # raises if the licence was not signed by the vendor key
metering = sys.argv[sys.argv.index("--metering-key") + 1] if "--metering-key" in sys.argv else None
with open(log, "a") as f:
    f.write(json.dumps({"action": action, "tenant": tenant, "path": path, "licence": json.loads(pad(payload)),
                        "metering_key": open(metering).read() if metering else None,
                        "metering_url": os.environ.get("CAIRN_METERING_URL")}) + "\n")
'''


@pytest.fixture
def api(services, settings, graph, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi.testclient import TestClient

    from cairn.app import create_api_app
    from cairn.services import build_services

    key = Ed25519PrivateKey.generate()
    (tmp_path / "licence.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    (tmp_path / "licence.pub").write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                                         serialization.PublicFormat.SubjectPublicKeyInfo))
    (tmp_path / "stripe").write_text("sk_test_123")
    (tmp_path / "whsec").write_text(WEBHOOK_SECRET)
    (tmp_path / "metering").write_text("metering-master-key-7d1e")
    (tmp_path / "provision.py").write_text(PROVISION)
    cfg = {"domain": "cairn.test", "public_url": "https://signup.cairn.test",
           "smtp": f"smtp://127.0.0.1:{services['smtp']}?starttls=false", "from": "hello@cairn.test",
           "stripe_key_file": str(tmp_path / "stripe"), "stripe_webhook_secret_file": str(tmp_path / "whsec"),
           "stripe_api": services["stripe"], "licence_key_file": str(tmp_path / "licence.pem"), "licence_kid": "test-1",
           "provision": [sys.executable, str(tmp_path / "provision.py")],
           "metering_key_file": str(tmp_path / "metering"),
           "meters": {"query_cpu_seconds": "cairn_cpu_seconds", "warehouse_node_seconds": "cairn_node_seconds",
                      "ai_usd_cents": "cairn_ai_cents"},
           "plans": {"team": {"price": "price_team", "edition": "team", "max_users": 25},
                     "business": {"price": "price_business", "edition": "enterprise", "max_users": 250}}}
    monkeypatch.setenv("CAIRN_SIGNUP", json.dumps(cfg))
    monkeypatch.setenv("PROVISION_LOG", str(tmp_path / "provisioned.jsonl"))
    monkeypatch.setenv("PROVISION_FAIL", str(tmp_path / "fail"))
    monkeypatch.setenv("LICENCE_PUB", str(tmp_path / "licence.pub"))
    httpx.delete(f"{services['mail']}/api/v1/messages")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    assert s.signup is not None
    return s, TestClient(create_api_app(s)), tmp_path, services


def mails(services, to: str, n: int) -> list[dict]:
    end = time.time() + 15
    while time.time() < end:
        msgs = httpx.get(f"{services['mail']}/api/v1/messages").json().get("messages") or []
        mine = [m for m in msgs if any(t["Address"] == to for t in m["To"])]
        if len(mine) >= n:
            return [httpx.get(f"{services['mail']}/api/v1/message/{m['ID']}").json() for m in reversed(mine)]
        time.sleep(0.3)
    raise AssertionError(f"{n} mails to {to} did not arrive")


def event(kind: str, obj: dict) -> dict:
    return {"id": f"evt_{kind}_{time.time_ns()}", "object": "event", "type": kind, "data": {"object": obj}}


def post(c, ev: dict, secret: str = WEBHOOK_SECRET, ts=None):
    from cairn.hosting.signup import stripe_signature

    body = json.dumps(ev).encode()
    return c.post("/v1/billing/stripe/webhook", content=body,
                  headers={"Stripe-Signature": stripe_signature(body, secret, ts), "Content-Type": "application/json"})


def provisioned(tmp_path) -> list[dict]:
    p = tmp_path / "provisioned.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_team_signs_up_pays_and_its_licence_follows_the_subscription(api):
    s, c, tmp, srv = api
    # Signing up: checked, then a confirmation link by email.
    for bad, msg in [({"email": "x", "organization": "Acme", "tenant": "acme", "plan": "team"}, "email"),
                     ({"email": "kim@acme.test", "organization": "Acme", "tenant": "admin", "plan": "team"}, "tenant"),
                     ({"email": "kim@acme.test", "organization": "Acme", "tenant": "A cme", "plan": "team"}, "tenant"),
                     ({"email": "kim@acme.test", "organization": "Acme", "tenant": "acme", "plan": "gold"}, "plan")]:
        r = c.post("/v1/signup", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], (bad, r.text)
    assert c.get("/v1/signup/available/acme").json()["available"] is True
    r = c.post("/v1/signup", json={"email": "Kim@Acme.test", "organization": "Acme", "tenant": "acme", "plan": "team"})
    assert r.status_code == 202, r.text
    assert c.get("/v1/signup/available/acme").json()["available"] is False
    assert c.post("/v1/signup", json={"email": "eve@evil.test", "organization": "Evil", "tenant": "acme",
                                      "plan": "team"}).status_code == 409
    [m] = mails(srv, "kim@acme.test", 1)
    assert m["Subject"] == "Confirm your Cairn workspace" and "acme.cairn.test" in m["Text"]
    token = re.search(r"verify\?token=([\w-]+)", m["Text"]).group(1)

    # Confirming sends the browser to Stripe Checkout (stripe-mock answers as Stripe would).
    r = c.get(f"/v1/signup/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://"), r.text
    assert c.get(f"/v1/signup/verify?token={token}", follow_redirects=False).status_code == 410   # once only
    from cairn.hosting.signup import signups

    with s.engine.connect() as conn:
        row = conn.execute(signups.select().where(signups.c.tenant == "acme")).first()
    assert row.status == "verified" and row.stripe_customer.startswith("cus_")

    # Stripe's webhook: only signed, recent events count.
    end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    paid = event("checkout.session.completed", {"object": "checkout.session", "client_reference_id": row.id,
                                                "customer": row.stripe_customer, "subscription": "sub_acme",
                                                "current_period_end": end})
    assert post(c, paid, secret="whsec_wrong").status_code == 400
    assert post(c, paid, ts=int(time.time()) - 3600).status_code == 400
    r = post(c, paid)
    assert r.status_code == 200 and r.json() == {"received": True, "handled": True}, r.text
    assert post(c, paid).json() == {"received": True, "duplicate": True}             # Stripe resends: once
    [created] = provisioned(tmp)
    lic = created["licence"]
    assert created["action"] == "create" and created["tenant"] == "acme" and not os.path.exists(created["path"])
    from cairn.hosting.metering import tenant_key
    assert created["metering_key"] == tenant_key("metering-master-key-7d1e", "acme")     # its usage-metering key
    assert created["metering_url"] == "https://signup.cairn.test"
    assert (lic["tenant"], lic["edition"], lic["max_users"], lic["kid"]) == ("acme", "team", 25, "test-1")
    assert lic["expires_at"] == datetime.fromtimestamp(end, tz=timezone.utc).date().isoformat()
    assert [x["Subject"] for x in mails(srv, "kim@acme.test", 2)][-1] == "Your Cairn workspace acme is ready"

    # Renewal extends the licence; a plan change reissues it for the new edition.
    later = end + 30 * 86400
    post(c, event("invoice.paid", {"object": "invoice", "subscription": "sub_acme", "customer": row.stripe_customer,
                                   "lines": {"data": [{"period": {"start": end, "end": later}}]}}))
    renewed = provisioned(tmp)[-1]
    assert renewed["action"] == "upgrade" and renewed["licence"]["expires_at"] > lic["expires_at"]
    post(c, event("customer.subscription.updated", {"object": "subscription", "id": "sub_acme",
                                                    "items": {"data": [{"price": {"id": "price_business"}}]}}))
    up = provisioned(tmp)[-1]["licence"]
    assert (up["edition"], up["max_users"]) == ("enterprise", 250)

    # Provisioning that fails is kept as failed and retried until it works.
    (tmp / "fail").write_text("1")
    post(c, event("invoice.paid", {"object": "invoice", "subscription": "sub_acme",
                                   "lines": {"data": [{"period": {"end": later + 30 * 86400}}]}}))
    with s.engine.connect() as conn:
        assert conn.execute(signups.select().where(signups.c.tenant == "acme")).first().status == "active"
    r2 = c.post("/v1/signup", json={"email": "lee@beta.test", "organization": "Beta", "tenant": "beta", "plan": "team"})
    assert r2.status_code == 202
    tok2 = re.search(r"verify\?token=([\w-]+)", mails(srv, "lee@beta.test", 1)[0]["Text"]).group(1)
    c.get(f"/v1/signup/verify?token={tok2}", follow_redirects=False)
    with s.engine.connect() as conn:
        beta = conn.execute(signups.select().where(signups.c.tenant == "beta")).first()
    post(c, event("checkout.session.completed", {"client_reference_id": beta.id, "customer": beta.stripe_customer,
                                                 "subscription": "sub_beta", "current_period_end": end}))
    with s.engine.connect() as conn:
        beta = conn.execute(signups.select().where(signups.c.tenant == "beta")).first()
    assert beta.status == "failed" and "kubernetes is unreachable" in beta.error
    (tmp / "fail").unlink()
    assert s.signup.retry_failed() == 1
    with s.engine.connect() as conn:
        assert conn.execute(signups.select().where(signups.c.tenant == "beta")).first().status == "active"

    # Cancellation: nothing renews the licence; the owner is told. A failed payment is told too.
    post(c, event("invoice.payment_failed", {"object": "invoice", "customer": row.stripe_customer}))
    post(c, event("customer.subscription.deleted", {"object": "subscription", "id": "sub_acme"}))
    subjects = [x["Subject"] for x in mails(srv, "kim@acme.test", 4)]
    assert "A Cairn payment did not go through" in subjects and "Your Cairn subscription has ended" in subjects
    with s.engine.connect() as conn:
        assert conn.execute(signups.select().where(signups.c.tenant == "acme")).first().status == "canceled"

    # The billing portal, through a link sent to the owner's address (and only to it).
    assert c.post("/v1/billing/portal-link", json={"email": "nobody@x.test"}).status_code == 202
    assert c.post("/v1/billing/portal-link", json={"email": "kim@acme.test"}).status_code == 202
    link = mails(srv, "kim@acme.test", 5)[-1]
    assert link["Subject"] == "Manage the billing of acme"
    ptoken = re.search(r"portal\?token=([\w-]+)", link["Text"]).group(1)
    r = c.get(f"/v1/billing/portal?token={ptoken}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://"), r.text
    assert c.get(f"/v1/billing/portal?token={ptoken}", follow_redirects=False).status_code == 410
    actions = {e["action"] for e in s.audit.entries("hosting", limit=20)}
    assert {"signup.verified", "tenant.licence_issued", "tenant.licence_renewed"} <= actions


def test_signup_attempts_are_limited(api):
    s, c, _, _ = api
    codes = [c.post("/v1/signup", json={"email": "spam@x.test", "organization": "S", "tenant": f"spam{i}",
                                        "plan": "team"}).status_code for i in range(7)]
    assert codes[:5] == [202] * 5 and codes[5] == 429
