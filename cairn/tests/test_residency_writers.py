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
    pytest.importorskip("numpy")                         # vector search needs it; the python job has none
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


# ---- clean-room uploads, mirrors and database mirrors ----------------------------------------------------------
PARTNER = "partner:acme"
UPLOADS_PINNED = {**RULES, "pins": [{"namespaces": ["cleanrooms"], "region": "eu"}]}


def rooms(engine, region):
    from cairn.dataaccess.cleanrooms import CleanRooms

    cr = CleanRooms(engine, untouched, lambda actor, party: actor == party, lambda actor, table: True,
                    write_table=untouched)
    cr.residency = Residency(UPLOADS_PINNED, region)
    return cr


@pytest.mark.parametrize("region", OUTSIDE)
def test_partners_upload_into_a_pinned_clean_room_namespace_only_where_the_pin_allows(engine, region):
    from cairn.dataaccess.cleanrooms import RoomError

    cr = rooms(engine, region)
    with pytest.raises(RoomError) as e:                 # a room with outside parties: they would upload here
        cr.create(ADMIN, {"name": "ads", "parties": [ADMIN, PARTNER]})
    assert e.value.status == 403 and "residency" in str(e.value)
    assert cr.create(ADMIN, {"name": "inside", "parties": [ADMIN, "urn:li:corpuser:bob"]})["name"] == "inside"

    rooms(engine, "eu").create(ADMIN, {"name": "ads", "parties": [ADMIN, PARTNER]})   # made where it was allowed
    with pytest.raises(RoomError) as e:                 # the file is not even read; write_table is untouched
        cr.upload(PARTNER, "ads", "spend", b"not,a,file", "csv")
    assert e.value.status == 403 and "residency" in str(e.value)


def mirrors(engine, region):
    from cairn.pipelines.mirror import Mirrors

    m = Mirrors(engine, Untouched(), lambda connection: "")
    m.residency = Residency(RULES, region)
    m._connect = untouched
    return m


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_mirror_into_a_pinned_namespace_reads_its_source_only_where_the_pin_allows(engine, region):
    pytest.importorskip("psycopg")
    from cairn.pipelines.mirror import MirrorError

    m = mirrors(engine, region)
    with pytest.raises(MirrorError) as e:
        m.create(ADMIN, {"name": "people", "connection": "crm", "table": "public.people", "target": "crm_eu.people"})
    assert e.value.status == 403 and "residency" in str(e.value)

    m._created(ADMIN, "m1", "people", "crm", "public.people", "crm_eu.people", ["id"])   # made before the pin
    assert m.run("people") == 0                         # the source is not connected to (untouched)
    got = m.get("people")
    assert got["status"] == "failed" and "residency" in got["error"]


@pytest.mark.parametrize("region", OUTSIDE)
def test_a_database_mirror_into_a_pinned_namespace_reads_its_source_only_where_the_pin_allows(engine, region):
    pytest.importorskip("psycopg")
    from sqlalchemy import insert, select

    from cairn.pipelines.mirror import MirrorError
    from cairn.pipelines.mirror_db import DatabaseMirrors, database_mirrors

    m = DatabaseMirrors(engine, Untouched(), lambda connection: "")
    m.residency = Residency(RULES, region)
    m._connect = untouched
    body = {"name": "crm", "connection": "crm", "schema": "public", "target_namespace": "crm_eu"}
    with pytest.raises(MirrorError) as e:
        m.create(ADMIN, body)
    assert e.value.status == 403 and "residency" in str(e.value)

    with engine.begin() as c:                           # made before the pin
        c.execute(insert(database_mirrors).values(
            id="d1", name="crm", connection="crm", schema="public", target_namespace="crm_eu", include='["*"]',
            exclude="[]", defaults="{}", tables="{}", enabled=True, status="running", error=None, last_lsn=None,
            last_commit_ms=None, created_by=ADMIN, created_ms=0))
    with pytest.raises(MirrorError) as e:
        m.update(ADMIN, "crm", {"include": ["people"]})
    assert e.value.status == 403 and "residency" in str(e.value)
    assert m.run("crm") == 0                            # the source is not connected to (untouched)
    with engine.connect() as c:
        row = c.execute(select(database_mirrors).where(database_mirrors.c.name == "crm")).first()
    assert row.status == "failed" and "residency" in row.error


def test_services_hand_the_residency_rules_to_every_writer(monkeypatch, tmp_path):
    """_wire_residency gives the rules to every writer built before it (samples and clean rooms take them where
    they are built, after it)."""
    import json
    import types

    from cairn.services import _wire_residency

    rules = tmp_path / "residency.json"
    rules.write_text(json.dumps(RULES))
    monkeypatch.setenv("CAIRN_RESIDENCY_FILE", str(rules))
    monkeypatch.setenv("CAIRN_REGION", "us")
    writers = ("pipes", "streams", "saas", "connector_syncs", "parquet_tables", "delta_convert", "vectors",
               "branches", "synthetic", "mirrors", "database_mirrors")
    s = types.SimpleNamespace(**{w: types.SimpleNamespace(residency=None) for w in writers})
    _wire_residency(s)
    assert s.residency.active and all(getattr(s, w).residency is s.residency for w in writers)
