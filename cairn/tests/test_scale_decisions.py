"""Scale, measured on the real code paths (in process, so the numbers are Cairn's, not the network's):
a cached policy decision, and Cairn Catalog's one-call decision for a job reading 1,000 tables, under a
policy set of realistic size. The limits are the roadmap's (L10), with headroom for a shared CI runner;
the measurements are printed for the record."""

import json
import statistics
import time

import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.governance.pdp import AuthorizeRequest, ResourceRef
from cairn.services import build_services

ANA = "urn:li:corpuser:ana"


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


@pytest.fixture
def s(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", "k" * 32)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", json.dumps({"lakehouse": {"platform": "iceberg", "database": ""}}))
    svc = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    # 60 policies: team grants by schema, masks, row filters and denies, as a mid-size organisation has.
    pols = []
    for i in range(20):
        pols.append(Policy(id=f"team{i}-read", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
                           target=Target(resource_urns=[f"*schema{i}.*"]),
                           condition={"attr": "subject.groups", "op": "contains", "value": f"urn:li:corpGroup:team{i}"}))
        pols.append(Policy(id=f"team{i}-mask", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
                           target=Target(resource_urns=[f"*schema{i}.*"]),
                           obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})]))
        pols.append(Policy(id=f"team{i}-deny", effect="deny", actions=["VIEW_DATASET_PROFILE"],
                           target=Target(resource_urns=[f"*schema{i}.secret_*"])))
    pols.append(Policy(id="ana-everything", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
                       condition={"attr": "subject.urn", "op": "eq", "value": ANA}))
    svc.pdp.set_bundle(PolicyBundle(policies=pols))
    return svc


def test_a_cached_decision_is_well_under_five_milliseconds(s):
    req = AuthorizeRequest(actor=ANA, privilege="VIEW_DATASET_PROFILE",
                           resource=ResourceRef(type="dataset", urn="urn:li:dataset:(urn:li:dataPlatform:iceberg,schema3.orders,PROD)"))
    cold = time.perf_counter()
    s.pdp.decide(req)
    cold = (time.perf_counter() - cold) * 1000
    times = []
    started = time.perf_counter()
    for _ in range(20_000):
        t = time.perf_counter()
        d = s.pdp.decide(req)
        times.append((time.perf_counter() - t) * 1000)
    rate = 20_000 / (time.perf_counter() - started)
    assert d.decision == "ALLOW"
    p50, p99 = statistics.median(times), _pct(times, 0.99)
    print(f"\npolicy decision: cold {cold:.2f} ms; cached p50 {p50:.4f} ms, p99 {p99:.4f} ms, {rate:,.0f}/s on one thread")
    assert p99 < 5 and rate > 2000                                     # the roadmap's target: p99 < 5 ms at 2k/s


def test_a_job_reading_a_thousand_tables_is_decided_in_one_call(s):
    tables = [{"namespace": [f"schema{i % 20}"], "name": (f"secret_{i}" if i % 50 == 0 else f"t{i}")} for i in range(1000)]
    times = []
    for _ in range(5):
        t = time.perf_counter()
        out = s.lakehouse.decide("ana", tables)
        times.append((time.perf_counter() - t) * 1000)
    assert len(out["tables"]) == 1000
    denied = [t["name"] for t, d in zip(tables, out["tables"]) if not d.get("read")]
    assert denied == [t["name"] for t in tables if t["name"].startswith("secret_")]   # every deny held, nothing else refused
    first, warm = times[0], statistics.median(times[1:])
    print(f"\n1,000-table decision: first {first:.0f} ms, warm {warm:.0f} ms")
    # The first call evaluates every table cold (about 5,000 decisions): about 0.6 s on a laptop, but up to
    # three times that on a busy shared CI runner. Cached calls are what jobs repeat, and stay strict.
    assert first < 3000 and warm < 500
