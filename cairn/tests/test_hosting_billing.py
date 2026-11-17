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
if os.path.exists(os.environ["PROVISION_LOG"] + ".fail"):
    print("kubernetes is unreachable", file=sys.stderr); sys.exit(1)
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


def _report(m, master, tenant, day, metrics):
    from cairn.hosting.metering import sign, tenant_key

    body = json.dumps({"tenant": tenant, "day": day, "metrics": metrics, "sent_ms": time.time_ns()}).encode()
    return m.receive(body, sign(tenant_key(master, tenant), body))


def test_a_settled_days_usage_is_frozen_once_billing_has_begun(cp):
    """Billing reads a settled day, sends it, then marks it billed. A report in between (or after a send whose
    answer was lost) replaced the value: Stripe got one figure and Cairn kept another, and the retry carried
    other parameters under the same idempotency key (refused for a day, then possibly counted again)."""
    from cairn.hosting.metering import Metering, tenant_usage

    _, engine, stripe, _, _ = cp
    m = Metering(engine, "master-1", {"query_cpu_seconds": "cairn_cpu"}, stripe, lambda t: "cus_9")
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    _report(m, "master-1", "acme", day, {"query_cpu_seconds": 100})     # a late first report is kept
    stripe.fail = True
    assert m.bill() == 0                                               # Stripe down: kept, with the error
    assert _report(m, "master-1", "acme", day, {"query_cpu_seconds": 7})["kept"] == 0
    stripe.fail = False
    assert m.bill() == 1
    [(path, data, idem)] = stripe.calls
    assert data["payload"]["value"] == "100" and idem == f"meter-acme-{day}-query_cpu_seconds"
    with engine.connect() as conn:
        assert conn.execute(tenant_usage.select()).first().value == 100
    # an unsettled day (yesterday, inside settle_hours) is still replaced by a newer report
    m.settle_hours = 48
    y = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _report(m, "master-1", "acme", y, {"query_cpu_seconds": 5})
    assert _report(m, "master-1", "acme", y, {"query_cpu_seconds": 6})["kept"] == 1


def test_a_report_billing_could_never_send_is_refused(cp):
    """A day older than Stripe's window, or a value no BIGINT holds, was accepted: the first was retried every
    billing tick for ever, the second failed the insert with a 500."""
    from cairn.hosting.metering import Metering, MeteringError

    _, engine, stripe, _, _ = cp
    m = Metering(engine, "master-1", {"query_cpu_seconds": "cairn_cpu"}, stripe, lambda t: "cus_9")
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    with pytest.raises(MeteringError) as e:
        _report(m, "master-1", "acme", old, {"query_cpu_seconds": 1})
    assert e.value.status == 400
    day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    with pytest.raises(MeteringError) as e:
        _report(m, "master-1", "acme", day, {"query_cpu_seconds": 2**63})
    assert e.value.status == 400
    assert _report(m, "master-1", "acme", day, {"query_cpu_seconds": 2**63 - 1})["kept"] == 1


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


def test_the_first_invoice_sets_the_licence_period_whichever_event_comes_first(cp):
    """Stripe does not order events: the first invoice.paid often comes before checkout.session.completed,
    whose session carries no period. The invoice was dropped (no signup had the subscription yet) and the
    licence ran for 31 days: a yearly customer's tenant turned read-only after a month and a half."""
    svc, engine, _, sent, tmp = cp
    year = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
    expected = datetime.fromtimestamp(year, tz=timezone.utc).date().isoformat()
    row = _verified(svc, engine, sent, plan="yearly")
    _hook(svc, "invoice.paid", {"object": "invoice", "subscription": "sub_y", "customer": row.stripe_customer,
                                "lines": {"data": [{"period": {"start": int(time.time()), "end": year}}]}})
    _hook(svc, "checkout.session.completed", {"object": "checkout.session", "client_reference_id": row.id,
                                              "customer": row.stripe_customer, "subscription": "sub_y",
                                              "payment_status": "paid"})
    row = _row(engine, "acme")
    assert (row.status, row.paid_until, row.stripe_subscription) == ("active", expected, "sub_y")
    assert [p["action"] for p in _provisioned(tmp)] == ["create"]
    # another customer's invoice changes nothing here
    _hook(svc, "invoice.paid", {"object": "invoice", "subscription": "sub_other", "customer": "cus_other",
                                "lines": {"data": [{"period": {"end": year + 86400 * 400}}]}})
    assert _row(engine, "acme").paid_until == expected


@pytest.mark.parametrize("kind", ["invoice.paid", "customer.subscription.updated"])
def test_a_renewal_whose_provisioning_failed_is_redone_when_stripe_resends_it(cp, kind):
    """The new period (or plan) was recorded before the licence was issued: when provisioning failed, Stripe's
    retry of the event found nothing to change and the paying tenant's licence was never renewed."""
    from cairn.hosting.signup import SignupError

    svc, engine, _, sent, tmp = cp
    end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    row = _verified(svc, engine, sent)
    _hook(svc, "checkout.session.completed", {"client_reference_id": row.id, "customer": row.stripe_customer,
                                              "subscription": "sub_a", "payment_status": "paid",
                                              "current_period_end": end})
    assert _row(engine, "acme").status == "active"
    if kind == "invoice.paid":
        obj = {"subscription": "sub_a", "customer": row.stripe_customer,
               "lines": {"data": [{"period": {"end": end + 30 * 86400}}]}}
    else:
        obj = {"id": "sub_a", "items": {"data": [{"price": {"id": "price_yearly"}}]}}
    (tmp / "provisioned.jsonl.fail").write_text("1")
    with pytest.raises(SignupError):
        _hook(svc, kind, obj, eid="evt_renew")
    (tmp / "provisioned.jsonl.fail").unlink()
    assert _hook(svc, kind, obj, eid="evt_renew")["handled"] is True         # Stripe delivers it again
    assert [p["action"] for p in _provisioned(tmp)] == ["create", "upgrade"]


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


@pytest.mark.parametrize("name", ["platform", "postgres", "jobs", "apps", "agents", "models"])
def test_names_whose_namespace_the_platform_uses_are_reserved(cp, name):
    """tenant.sh puts a tenant in namespace cairn-<tenant>: "platform" is cairn-platform, where the shared
    PostgreSQL runs. Such a signup was taken and paid for, then could never be provisioned."""
    from cairn.hosting.signup import SignupError

    svc = cp[0]
    with pytest.raises(SignupError) as e:
        svc.start({"email": "kim@acme.test", "organization": "Acme", "tenant": name, "plan": "team"}, "10.0.0.1")
    assert e.value.status == 400 and "tenant" in str(e.value)
