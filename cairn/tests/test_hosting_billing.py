"""Hosted signup, Stripe's webhook and usage metering on the control plane, without docker: SQLite for the
control plane's tables, a recording stand-in for Stripe's API, mails captured, and a provisioning command that
logs what it was asked. The end-to-end versions (stripe-mock, Mailpit) are test_signup_e2e.py and
test_metering_e2e.py."""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")

WHSEC = "whsec_unit_5c2a"

PROVISION = r'''
import json, os, sys
with open(os.environ["PROVISION_LOG"], "a") as f:
    f.write(json.dumps({"action": sys.argv[1], "tenant": sys.argv[2]}) + "\n")
'''


class FakeStripe:
    def __init__(self):
        self.calls, self.fail = [], False

    def post(self, path, data, idempotency_key=None):
        if self.fail:
            from cairn.hosting.signup import SignupError

            raise SignupError("Stripe answered 503", 502)
        self.calls.append((path, data, idempotency_key))
        n = len(self.calls)
        if path == "/v1/customers":
            return {"id": f"cus_{n}"}
        if path == "/v1/checkout/sessions":
            return {"id": f"cs_{n}", "url": f"https://checkout.stripe.test/{n}"}
        return {"id": f"x_{n}", "url": "https://billing.stripe.test/p"}


@pytest.fixture
def cp(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from sqlalchemy import create_engine

    from cairn.hosting.metering import tenant_usage
    from cairn.hosting.signup import Signup, signups, stripe_events

    engine = create_engine(f"sqlite:///{tmp_path / 'cp.db'}")
    for t in (signups, stripe_events, tenant_usage):
        t.create(engine)
    key = Ed25519PrivateKey.generate()
    (tmp_path / "licence.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    (tmp_path / "whsec").write_text(WHSEC)
    (tmp_path / "metering").write_text("metering-master-9a")
    (tmp_path / "provision.py").write_text(PROVISION)
    monkeypatch.setenv("PROVISION_LOG", str(tmp_path / "provisioned.jsonl"))
    cfg = {"domain": "cairn.test", "public_url": "https://signup.cairn.test", "smtp": "smtp://127.0.0.1:1",
           "from": "hello@cairn.test", "stripe_key_file": str(tmp_path / "whsec"),
           "stripe_webhook_secret_file": str(tmp_path / "whsec"), "licence_key_file": str(tmp_path / "licence.pem"),
           "licence_kid": "unit-1", "provision": [sys.executable, str(tmp_path / "provision.py")],
           "metering_key_file": str(tmp_path / "metering"),
           "plans": {"team": {"price": "price_team", "edition": "team", "max_users": 25},
                     "yearly": {"price": "price_yearly", "edition": "team", "max_users": 25}}}
    stripe = FakeStripe()
    svc = Signup(engine, cfg, stripe=stripe)
    sent = []
    svc.mail = lambda to, subject, body: sent.append({"to": to, "subject": subject, "body": body})
    return svc, engine, stripe, sent, tmp_path


def _row(engine, tenant):
    from cairn.hosting.signup import signups

    with engine.connect() as conn:
        return conn.execute(signups.select().where(signups.c.tenant == tenant)).first()


def _token(sent):
    return sent[-1]["body"].split("token=")[1].split()[0]


def _hook(svc, kind, obj, eid=None):
    from cairn.hosting.signup import stripe_signature

    body = json.dumps({"id": eid or f"evt_{time.time_ns()}", "type": kind, "data": {"object": obj}}).encode()
    return svc.webhook(body, stripe_signature(body, WHSEC))


def _provisioned(tmp_path):
    p = tmp_path / "provisioned.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _verified(svc, engine, sent, tenant="acme", email="kim@acme.test", plan="team"):
    svc.start({"email": email, "organization": "Acme", "tenant": tenant, "plan": plan}, "10.0.0.1")
    svc.verify(_token(sent))
    return _row(engine, tenant)


# --------------------------------------------------------------------------- metering
def test_metering_refuses_an_empty_master_key():
    """An empty metering key would make every tenant's key HMAC("", tenant): anyone could sign any tenant's usage."""
    from cairn.hosting.metering import Metering, tenant_key

    with pytest.raises(ValueError):
        tenant_key("", "acme")
    with pytest.raises(ValueError):
        Metering(None, "", {}, None, lambda t: "cus_1")


# --------------------------------------------------------------------------- Stripe's webhook
def test_a_checkout_completed_without_payment_does_not_provision(cp):
    """Delayed payment methods (SEPA, ACH) complete the Checkout with payment_status "unpaid": the tenant waits
    for checkout.session.async_payment_succeeded, and nothing is provisioned if the payment fails."""
    svc, engine, _, sent, tmp = cp
    end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    for tenant, outcome in (("acme", "checkout.session.async_payment_succeeded"),
                            ("beta", "checkout.session.async_payment_failed")):
        row = _verified(svc, engine, sent, tenant=tenant, email=f"kim@{tenant}.test")
        session = {"object": "checkout.session", "client_reference_id": row.id, "customer": row.stripe_customer,
                   "subscription": f"sub_{tenant}", "payment_status": "unpaid", "current_period_end": end}
        assert _hook(svc, "checkout.session.completed", session)["handled"] is True
        assert [p for p in _provisioned(tmp) if p["tenant"] == tenant] == []
        assert _row(engine, tenant).status == "verified"
        _hook(svc, outcome, {**session, "payment_status": "paid" if outcome.endswith("succeeded") else "unpaid"})
    assert [(p["action"], p["tenant"]) for p in _provisioned(tmp)] == [("create", "acme")]
    assert _row(engine, "acme").status == "active" and _row(engine, "beta").status == "verified"


# --------------------------------------------------------------------------- signing up
def test_an_unconfirmed_signup_does_not_hold_a_name_once_its_link_expired(cp):
    """Anyone could sign up for any name with an address they never confirm; the name stayed taken for ever."""
    from sqlalchemy import update

    from cairn.hosting.signup import SignupError, signups

    svc, engine, _, sent, _ = cp
    svc.start({"email": "squat@evil.test", "organization": "Evil", "tenant": "acme", "plan": "team"}, "10.0.0.9")
    with pytest.raises(SignupError) as e:           # while the link is good, the name is held
        svc.start({"email": "kim@acme.test", "organization": "Acme", "tenant": "acme", "plan": "team"}, "10.0.0.1")
    assert e.value.status == 409
    with engine.begin() as conn:
        conn.execute(update(signups).where(signups.c.tenant == "acme").values(token_expires_ms=int(time.time() * 1000) - 1))
    svc.start({"email": "kim@acme.test", "organization": "Acme", "tenant": "acme", "plan": "team"}, "10.0.0.1")
    row = _row(engine, "acme")
    assert (row.email, row.organization, row.status) == ("kim@acme.test", "Acme", "pending")
    assert sent[-1]["to"] == "kim@acme.test"
