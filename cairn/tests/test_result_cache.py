"""The shared result cache's key (cairn.sqlworkspace.cache): two people share a cached result only when everything
that shapes what they see of every table read is the same. The end-to-end test on real Trino is in
tests/test_trino_e2e.py; these check the fingerprint's inputs on their own."""

from __future__ import annotations

from sqlalchemy import create_engine

from cairn.common.db import metadata
from cairn.governance.pdp import Decision
from cairn.sqlworkspace.cache import ResultCache, policy_decisions, result_cache

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)"


class _Pdp:
    def decide(self, req):          # the same decision for everyone: may read, no obligations
        return Decision(decision="ALLOW", reason="test")


class _Consent:
    """Bob's group has a purpose with opt-in consent: he sees only the rows of people who agreed. Alice has none."""
    def filters(self, actor, urn, dialect, groups=None):
        return ["(lower(to_hex(sha256(to_utf8(email)))) IN ('ab'))"] if actor.endswith(":bob") else []


def _cache(consent=None, urns=(ORDERS,)):
    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[result_cache])
    decisions = policy_decisions(_Pdp(), lambda actor, urn: True, lambda: consent)
    return ResultCache(engine, lambda sql, conn: list(urns), decisions)


def _put_and_get(cache, first: str, second: str, sql: str = "SELECT * FROM sales.orders"):
    hit, ticket = cache.get(first, sql, "lakehouse", 100)
    assert hit is None and ticket is not None
    cache.put(ticket, {"columns": ["email"], "rows": [["everyone@example.com"]]})
    return cache.get(second, sql, "lakehouse", 100)[0]


def test_people_with_the_same_decisions_share_a_result():
    assert _put_and_get(_cache(), "urn:li:corpuser:alice", "urn:li:corpuser:carol")["cached"] is True


def test_consent_filters_keep_results_apart():
    """Alice has no consent purpose (every row); Bob's purpose filters out the people who did not agree. Their policy
    decisions are the same, so without the consent filters in the key Bob was served Alice's rows."""
    assert _put_and_get(_cache(_Consent()), "urn:li:corpuser:alice", "urn:li:corpuser:bob") is None
    assert _put_and_get(_cache(_Consent()), "urn:li:corpuser:bob", "urn:li:corpuser:alice") is None


def test_system_tables_are_never_cached():
    """System tables show a person only their own rows (and redact typed text) unless they hold an operations
    privilege, which the dataset's decisions do not show: a cached result would hand one person's query history,
    or an administrator's whole table, to the next."""
    history = "urn:li:dataset:(urn:li:dataPlatform:trino,cairn_system.trino.queries,PROD)"
    cache = _cache(urns=(history,))
    assert cache.get("urn:li:corpuser:alice", "SELECT * FROM trino.queries", "system", 100) == (None, None)
    joined = _cache(urns=(ORDERS, history.replace("cairn_system", "CAIRN_SYSTEM")))
    assert joined.get("urn:li:corpuser:alice", "SELECT 1", "lakehouse", 100) == (None, None)
