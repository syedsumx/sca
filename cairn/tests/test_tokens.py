"""Reversible tokenisation: deterministic keyed tokens, a vault given back only with DETOKENIZE and a purpose,
erasure by forgetting, and master-key rotation that changes no token. On SQLite and, with CAIRN_TEST_PG_URL,
PostgreSQL, with the real policy decision point."""

from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import create_engine, select

from cairn.common.audit import AuditLog
from cairn.common.db import init_db
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef
from cairn.governance.tokens import TOKEN, TokenError, Tokens, token_vault

ANA, BOB, LOADER = (f"urn:li:corpuser:{n}" for n in ("ana", "bob", "loader"))
KEY1, KEY2 = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))


def _tokens(engine, graph, monkeypatch, key=KEY1, previous=None) -> Tokens:
    monkeypatch.setenv("CAIRN_TOKENS_KEY", key)
    if previous:
        monkeypatch.setenv("CAIRN_TOKENS_KEY_PREVIOUS", previous)
    pdp = PolicyDecisionPoint(AttributeResolver(graph), "authoritative", [], 0)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana-reveals-emails", effect="allow", actions=["DETOKENIZE"], condition=sub(ANA),
               target=Target(resource_urns=["urn:li:tokenDomain:email"])),
        Policy(id="loader-tokenises", effect="allow", actions=["TOKENIZE"], condition=sub(LOADER),
               target=Target(resource_urns=["urn:li:tokenDomain:*"]))]))

    def may(actor, privilege, urn):
        return pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege,
                                           resource=ResourceRef(type="tokenDomain", urn=urn))).decision == "ALLOW"
    return Tokens(engine, AuditLog(engine), may)


def _check(engine, graph, monkeypatch):
    init_db(engine)
    t = _tokens(engine, graph, monkeypatch)
    t.create_domain(ANA, "email", "customer e-mail addresses")
    t.create_domain(ANA, "iban")
    with pytest.raises(TokenError) as e:
        t.create_domain(ANA, "email")
    assert e.value.status == 409
    assert [d["name"] for d in t.domains()] == ["email", "iban"]

    # Deterministic per domain, keyed (not a plain hash), and None stays None.
    a = t.tokenize_as(LOADER, "email", ["jane@acme.com", "bo@example.org", "jane@acme.com", None])
    assert a[0] == a[2] and a[0] != a[1] and a[3] is None and all(TOKEN.match(x) for x in a[:3])
    assert t.tokenize("iban", ["jane@acme.com"])[0] != a[0]                    # another domain, another token
    import hashlib

    assert hashlib.sha256(b"jane@acme.com").hexdigest()[:26] not in a[0]
    with engine.connect() as c:
        stored = c.execute(select(token_vault.c.ciphertext).where(token_vault.c.domain == "email")).scalars().all()
    assert len(stored) == 2 and not any("jane" in s or "acme" in s for s in stored)   # encrypted, once each
    with pytest.raises(TokenError) as e:
        t.tokenize_as(BOB, "email", ["x"])                                          # no TOKENIZE
    assert e.value.status == 403

    # Given back only with DETOKENIZE on the domain, for a stated purpose; audited without values.
    assert t.detokenize(ANA, "email", [a[1], a[0], "tok_" + "a" * 26], "fraud case 4471 review") == [
        "bo@example.org", "jane@acme.com", None]
    for actor, domain, purpose, status in ((BOB, "email", "fraud case 4471", 403), (ANA, "iban", "fraud case 4471", 403),
                                           (ANA, "email", "", 400)):
        with pytest.raises(TokenError) as e:
            t.detokenize(actor, domain, [a[0]], purpose)
        assert e.value.status == status
    with pytest.raises(TokenError):
        t.detokenize(ANA, "email", ["jane@acme.com"], "fraud case 4471 review")    # only tokens are taken
    log = AuditLog(engine).entries(category="tokens", limit=50)
    reveal = [x for x in log if x["action"] == "token.detokenized"]
    assert reveal and reveal[0]["details"]["purpose"] == "fraud case 4471 review" and reveal[0]["details"]["count"] == 3
    assert "jane" not in repr(log) and a[0] not in repr(log)

    # A new master key: domain keys re-wrapped, tokens unchanged, the vault still opens.
    t2 = _tokens(engine, graph, monkeypatch, key=KEY2, previous=KEY1)
    assert t2.rotate(ANA) == {"rewrapped": 2}
    monkeypatch.delenv("CAIRN_TOKENS_KEY_PREVIOUS")
    t3 = _tokens(engine, graph, monkeypatch, key=KEY2)
    assert t3.tokenize("email", ["jane@acme.com"]) == [a[0]]
    assert t3.detokenize(ANA, "email", [a[0]], "fraud case 4471 review") == ["jane@acme.com"]
    with pytest.raises(TokenError, match="no longer has"):
        _tokens(engine, graph, monkeypatch, key=KEY1)._domain_keys("email")     # the old key alone opens nothing

    # Forgetting a value: its token stays in the tables but is never given back again.
    assert t3.forget(ANA, "email", values=["jane@acme.com"]) == {"forgotten": 1}
    assert t3.detokenize(ANA, "email", [a[0], a[1]], "fraud case 4471 review") == [None, "bo@example.org"]
    assert t3.forget(ANA, "email", tokens=[a[1]]) == {"forgotten": 1}

    # Without a key the feature is off, never unencrypted.
    monkeypatch.delenv("CAIRN_TOKENS_KEY")
    off = Tokens(engine, None, lambda *a: True)
    with pytest.raises(TokenError) as e:
        off.tokenize("email", ["x"])
    assert e.value.status == 503


def test_tokens_on_sqlite(graph, tmp_path, monkeypatch):
    _check(create_engine(f"sqlite:///{tmp_path}/t.db"), graph, monkeypatch)


def test_tokens_on_postgres(graph, monkeypatch):
    url = os.environ.get("CAIRN_TEST_PG_URL")
    if not url:
        pytest.skip("CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
    from sqlalchemy import text

    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    _check(engine, graph, monkeypatch)


def test_tables_and_rows_are_tokenised_column_by_column(graph, tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")      # (the python CI job has no pyarrow; pipes and streams cover it there too)

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    t = _tokens(engine, graph, monkeypatch)
    t.create_domain(ANA, "email")
    with pytest.raises(TokenError):
        t.check_columns({"email": "nope"})
    data = pa.table({"id": [1, 2], "email": ["a@x.io", None]})
    out = t.tokenize_table(data, t.check_columns({"email": "email", "missing": "email"}))
    assert out.column("id").to_pylist() == [1, 2] and out.schema.field("email").type == pa.string()
    tok = out.column("email").to_pylist()
    assert TOKEN.match(tok[0]) and tok[1] is None
    rows = t.tokenize_rows([{"id": 1, "email": "a@x.io"}, {"id": 2}], {"email": "email"})
    assert rows == [{"id": 1, "email": tok[0]}, {"id": 2}]
