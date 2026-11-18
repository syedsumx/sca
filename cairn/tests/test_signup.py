"""Signup's names without Stripe or a mail server (tests/test_signup_e2e.py has the real ones)."""

import pytest
from sqlalchemy import update

from cairn.common.db import get_engine, init_db
from cairn.hosting.signup import Signup, SignupError, signups


class FakeStripe:
    def post(self, path, data, idempotency_key=None):
        if path == "/v1/customers":
            return {"id": "cus_1"}
        return {"url": "https://checkout.stripe.test/s"}


@pytest.fixture
def svc(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    s = Signup(engine, {"domain": "cairn.test", "public_url": "https://signup.cairn.test",
                        "plans": {"team": {"price": "price_1", "edition": "team", "max_users": 5}}}, stripe=FakeStripe())
    s.sent = []
    s.mail = lambda to, subject, body: s.sent.append(body)
    return s


def token(svc) -> str:
    return svc.sent[-1].split("token=")[1].split()[0]


def body(email):
    return {"email": email, "organization": "Acme", "tenant": "acme", "plan": "team"}


def test_a_name_is_kept_for_its_address_until_paid_and_freed_when_never_confirmed(svc):
    svc.start(body("kim@acme.test"), "1")
    with pytest.raises(SignupError, match="taken"):
        svc.start(body("eve@evil.test"), "2")
    # Kim confirms but leaves Stripe's Checkout: she may start again (her Stripe customer is kept).
    svc.verify(token(svc))
    svc.start(body("kim@acme.test"), "1")
    with svc.engine.connect() as conn:
        assert conn.execute(signups.select()).first().stripe_customer == "cus_1"
    # An address that never confirms holds the name only while its link is good; then anyone may take it, without
    # the previous address's Stripe customer.
    with pytest.raises(SignupError, match="taken"):
        svc.start(body("eve@evil.test"), "2")
    with svc.engine.begin() as conn:
        conn.execute(update(signups).values(token_expires_ms=0))
    svc.start(body("eve@evil.test"), "2")
    with svc.engine.connect() as conn:
        row = conn.execute(signups.select()).first()
    assert row.email == "eve@evil.test" and row.stripe_customer is None
    # Once paid for, a name is nobody else's.
    with svc.engine.begin() as conn:
        conn.execute(update(signups).values(status="active"))
    for who in ("eve@evil.test", "kim@acme.test"):
        with pytest.raises(SignupError, match="taken"):
            svc.start(body(who), "3")
