"""Data residency covers what Cairn itself writes: a pipe or a stream loads into a namespace pinned to a region only
on a deployment whose region (CAIRN_REGION) the pin allows, as Trino's reads and writes do. Checked when it is
defined and again for every file and batch (a pin added after the pipe was made still stops it), before anything
is read or written. No store, catalog or Kafka: a refusal happens before any of them is reached."""

from __future__ import annotations

import json
import types

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.governance.residency import Residency
from cairn.pipelines.pipes import PipeError, Pipes
from cairn.pipelines.streams import Streams

RULES = {"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
         "pins": [{"namespaces": ["crm_eu"], "region": "eu"}]}
ADMIN = "urn:li:corpuser:admin"


class Untouched:
    """A catalog that must not be reached."""

    def __call__(self):
        raise AssertionError("the catalog was reached: the residency check came too late")


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(e)
    return e


@pytest.mark.parametrize("region", ["us", None])
def test_a_pipe_into_a_pinned_namespace_loads_only_where_the_pin_allows(engine, region):
    p = Pipes(engine, {}, {})
    p.residency = Residency(RULES, region)
    body = {"name": "people", "bucket": "landing", "prefix": "people/", "format": "json", "target": "crm_eu.people"}
    with pytest.raises(PipeError) as e:
        p.create(body, ADMIN)
    assert e.value.status == 403 and "residency" in str(e.value)
    assert p.create({**body, "name": "raw", "target": "raw.people"}, ADMIN)["target"] == "raw.people"

    # A pipe made before the pin (or on another deployment) is refused when it loads, before the catalog.
    p._catalog = Untouched()
    pa = pytest.importorskip("pyarrow")
    with pytest.raises(PipeError, match="residency"):
        p._append({"target": "crm_eu.people", "format": "json", "create_table": True, "tokenize": {}},
                  pa.table({"id": [1]}))

    allowed = Pipes(engine, {}, {})
    allowed.residency = Residency(RULES, "eu")
    assert allowed.create({**body, "name": "eu-people"}, ADMIN)["target"] == "crm_eu.people"


@pytest.mark.parametrize("region", ["us", None])
def test_a_stream_into_a_pinned_namespace_writes_only_where_the_pin_allows(engine, region):
    s = Streams(engine, {}, {})
    s.residency = Residency(RULES, region)
    body = {"name": "people", "format": "http", "target": "crm_eu.people", "mode": "append"}
    with pytest.raises(PipeError) as e:
        s.create(body, ADMIN)
    assert e.value.status == 403 and "residency" in str(e.value)

    s._catalog = Untouched()
    pytest.importorskip("pyarrow")
    pytest.importorskip("pyiceberg")
    stream ={"id": "s1", "name": "people", "topic": "t", "format": "json", "mode": "append", "key": [],
              "target": "crm_eu.people", "tokenize": {}, "transform": None}
    msg = types.SimpleNamespace(partition=0, offset=0, key=None, value=json.dumps({"id": 1}).encode())
    with pytest.raises(PipeError, match="residency"):
        s.write(stream, [msg])


def test_a_stream_defined_in_an_allowed_region_is_accepted(engine):
    body = {"name": "people", "format": "http", "target": "crm_eu.people", "mode": "append"}
    allowed = Streams(engine, {}, {})
    allowed.residency = Residency(RULES, "eu")
    assert allowed.create(body, ADMIN)["target"] == "crm_eu.people"
