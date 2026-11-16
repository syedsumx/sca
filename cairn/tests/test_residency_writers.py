"""Data residency covers every writer inside Cairn, not only pipes and streams (tests/test_residency_ingest.py):
connector and SaaS syncs, Parquet registrations, Delta conversions, vector indexes, sample and synthetic data,
clean-room uploads, mirrors, database mirrors and table branches write into a namespace pinned to a region only on
a deployment whose region (CAIRN_REGION) the pin allows, and nowhere when the region is unset. Each is refused when
it is defined (403) and checked again before every run reads its source or writes, so one defined before the pin
(or on another deployment) stops too. No store, catalog, container or database: a refusal happens before any of
them is reached, which the fakes below assert."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.governance.residency import Residency

RULES = {"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
         "pins": [{"namespaces": ["crm_eu"], "region": "eu"}]}
ADMIN = "urn:li:corpuser:admin"
IMAGE = "registry.example.com/airbyte/source-faker@sha256:" + "a" * 64
OUTSIDE = ["us", None]                     # another region, and a deployment with no region: both refused


def untouched(*_a, **_k):
    raise AssertionError("reached past the residency check: it came too late")


class Untouched:
    """A catalog, runtime or source that must not be reached."""

    def __getattr__(self, name):
        return untouched

    def __call__(self, *a, **k):
        untouched()


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(e)
    return e


# ---- connector syncs and SaaS syncs ----------------------------------------------------------------------------
def connectors(engine, monkeypatch, region, runtime=None):
    from cairn.pipelines.connectors import ConnectorSyncs

    monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", IMAGE)
    c = ConnectorSyncs(engine, {}, untouched, lambda owner, urn: True, runtime)
    c.residency = Residency(RULES, region)
    return c


def connector_body(target: str, name: str = "people") -> dict:
    return {"name": name, "image": IMAGE, "config": {}, "secret_scope": "keys",
            "streams": [{"name": "people", "target": target, "mode": "append"}]}


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_connector_sync_into_a_pinned_namespace_runs_only_where_the_pin_allows(engine, monkeypatch, region):
    from cairn.pipelines.connectors import ConnectorError

    c = connectors(engine, monkeypatch, region)
    with pytest.raises(ConnectorError) as e:
        c.save(ADMIN, connector_body("crm_eu.people"))
    assert e.value.status == 403 and "residency" in str(e.value)
    assert c.save(ADMIN, connector_body("raw.people", "raw"))["streams"][0]["target"] == "raw.people"

    # Saved before the pin (or on another deployment): the run stops before the container starts or a key is read.
    before = connectors(engine, monkeypatch, "eu", runtime=Untouched())
    before.save(ADMIN, connector_body("crm_eu.people", "eu-people"))
    c.runtime = Untouched()
    r = c.run_now(ADMIN, "eu-people", wait=True)
    assert r["status"] == "failed" and "residency" in r["error"]
    # And each batch is checked before the catalog is reached.
    c._catalog = untouched
    with pytest.raises(ConnectorError, match="residency"):
        c._commit({"name": "people", "target": "crm_eu.people", "mode": "append"}, [{"id": 1}])


def test_a_connector_sync_is_saved_where_the_pin_allows(engine, monkeypatch):
    c = connectors(engine, monkeypatch, "eu")
    assert c.save(ADMIN, connector_body("crm_eu.people"))["streams"][0]["target"] == "crm_eu.people"


def saas(engine, region):
    from cairn.pipelines.saas import SaasSyncs

    s = SaasSyncs(engine, {}, untouched, lambda owner, urn: True)
    s.residency = Residency(RULES, region)
    return s


def saas_body(target: str, name: str = "accounts") -> dict:
    return {"name": name, "kind": "salesforce", "object": "Account", "fields": ["Name"], "target": target,
            "secret_scope": "crm-keys"}


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_saas_sync_into_a_pinned_namespace_runs_only_where_the_pin_allows(engine, region):
    from cairn.pipelines.saas import SaasError

    s = saas(engine, region)
    with pytest.raises(SaasError) as e:
        s.save(ADMIN, saas_body("crm_eu.accounts"))
    assert e.value.status == 403 and "residency" in str(e.value)
    assert s.save(ADMIN, saas_body("raw.accounts", "raw"))["target"] == "raw.accounts"

    saas(engine, "eu").save(ADMIN, saas_body("crm_eu.accounts", "eu-accounts"))   # saved where it was allowed
    r = s.run_now(ADMIN, "eu-accounts", wait=True)                                 # keys never read (untouched)
    assert r["status"] == "failed" and "residency" in r["error"]
    s._catalog = untouched
    sync = {"name": "eu-accounts", "target": "crm_eu.accounts", "fields": []}
    pytest.importorskip("pyarrow")
    with pytest.raises(SaasError, match="residency"):
        s._commit(sync, [])
    with pytest.raises(SaasError, match="residency"):
        s._replace(sync, None, {})


def test_a_saas_sync_is_saved_where_the_pin_allows(engine):
    assert saas(engine, "eu").save(ADMIN, saas_body("crm_eu.accounts"))["target"] == "crm_eu.accounts"


# ---- tables Cairn makes in the lakehouse: Parquet, Delta, vectors, samples, synthetic data, branches -------------
@pytest.mark.parametrize("region", OUTSIDE)
def test_parquet_files_are_registered_into_a_pinned_namespace_only_where_the_pin_allows(engine, region):
    from cairn.pipelines.parquet_tables import ParquetError, ParquetTables

    pt = ParquetTables(engine, {}, {}, buckets=["landing"], may_read=untouched)
    pt.residency = Residency(RULES, region)
    pt._fs = pt._catalog = untouched
    with pytest.raises(ParquetError) as e:
        pt.register({"name": "people", "bucket": "landing", "prefix": "people/", "target": "crm_eu.people"}, ADMIN)
    assert e.value.status == 403 and "residency" in str(e.value)
    # Registered before the pin: a refresh stops before the store is listed or the catalog reached.
    with pytest.raises(ParquetError, match="residency"):
        pt._refresh({"name": "people", "target": "crm_eu.people", "bucket": "landing", "prefix": "people/",
                     "created_by": ADMIN})


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_delta_table_is_converted_into_a_pinned_namespace_only_where_the_pin_allows(region):
    pytest.importorskip("pyiceberg")
    from cairn.pipelines.delta_convert import DeltaConversions
    from cairn.pipelines.pipes import PipeError

    conv = DeltaConversions({}, {})
    conv.residency = Residency(RULES, region)
    conv._fs = conv._catalog = untouched
    with pytest.raises(PipeError) as e:
        conv.convert("s3://lake/delta/people", "crm_eu.people", ADMIN)
    assert e.value.status == 403 and "residency" in str(e.value)


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_vector_index_over_a_pinned_table_is_made_refreshed_and_searched_only_where_the_pin_allows(engine, region):
    from cairn.pipelines.pipes import PipeError
    from cairn.pipelines.vectors import VectorIndexes

    body = {"name": "tickets", "source": "crm_eu.tickets", "key": "id", "text": "body"}
    vx = VectorIndexes(engine, {}, Untouched())
    vx.residency = Residency(RULES, region)
    with pytest.raises(PipeError) as e:
        vx.create(body, ADMIN)
    assert e.value.status == 403 and "residency" in str(e.value)

    eu = VectorIndexes(engine, {}, Untouched())
    eu.residency = Residency(RULES, "eu")
    ix = eu.create(body, ADMIN)                       # made where it was allowed
    vx._catalog = untouched
    with pytest.raises(PipeError, match="residency"):
        vx.refresh("tickets")
    with pytest.raises(PipeError, match="residency"):
        vx._matrix(ix)


@pytest.mark.parametrize("region", OUTSIDE)
def test_sample_data_loads_into_a_pinned_namespace_only_where_the_pin_allows(engine, region):
    from cairn.pipelines.samples import SampleError, Samples

    s = Samples(engine, {"uri": "http://catalog"})
    s.residency = Residency({**RULES, "pins": [{"namespaces": ["samples"], "region": "eu"}]}, region)
    s._catalog = untouched
    with pytest.raises(SampleError) as e:
        s.load(ADMIN, "retail")
    assert e.value.status == 403 and "residency" in str(e.value)


@pytest.mark.parametrize("region", OUTSIDE)
def test_synthetic_data_is_made_from_or_into_a_pinned_namespace_only_where_the_pin_allows(region):
    pytest.importorskip("pyiceberg")
    from cairn.pipelines.synthetic import Synthetic, SyntheticError

    s = Synthetic({}, lambda actor, privilege, urn: True, lambda ns, t: f"{ns}.{t}", untouched, "lakehouse",
                  untouched, untouched)
    s.residency = Residency(RULES, region)
    s._catalog = untouched
    for source, target in (("raw.orders", "crm_eu.orders"), ("crm_eu.orders", "dev.orders")):
        with pytest.raises(SyntheticError) as e:
            s.generate(ADMIN, source, target, 10)
        assert e.value.status == 403 and "residency" in str(e.value)


@pytest.mark.parametrize("region", OUTSIDE)
def test_branches_and_tags_of_a_pinned_table_change_only_where_the_pin_allows(region):
    from cairn.pipelines.branches import BranchError, Branches

    b = Branches({}, lambda actor, privilege, urn: True, lambda ns, t: f"{ns}.{t}", untouched, "lakehouse")
    b.residency = Residency(RULES, region)
    b._catalog = untouched
    for call in (lambda: b.create_ref(ADMIN, "crm_eu.people", "dev"), lambda: b.drop_ref(ADMIN, "crm_eu.people", "dev"),
                 lambda: b.publish(ADMIN, "crm_eu.people", "dev")):
        with pytest.raises(BranchError) as e:
            call()
        assert e.value.status == 403 and "residency" in str(e.value)
