"""Connector SDK: the test kit catches what Cairn Core or people would trip over, SDK connectors show
in the gallery from their manifest, and their recipes run through the bridge."""

import os
import random
import sys

import pytest
from pydantic import BaseModel, SecretStr

from cairn.sdk import Dataset, Field, Lineage, Source, to_mcps
from cairn.sdk.bridge import connection, proposals
from cairn.sdk.manifest import BRIDGE
from cairn.sdk.testkit import check
from cairn.sources.catalog import all_connectors, build_recipe, form_values

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "connectors", "csv-folder")


class Cfg(BaseModel):
    token: SecretStr
    n: int = 2


class Good(Source):
    platform = "acme"
    config_model = Cfg

    def datasets(self, config):
        for i in range(config.n):
            yield Dataset(name=f"db.t{i}", fields=[Field(path="id", type="number")], owners=["ana"], tags=["Gold"],
                          upstreams=[Lineage(upstream="urn:li:dataset:(urn:li:dataPlatform:postgres,db.raw,PROD)")])


class Leaky(Good):
    def datasets(self, config):
        yield Dataset(name="db.t", properties={"auth": config.token.get_secret_value()})


class Flaky(Good):
    def datasets(self, config):
        yield Dataset(name="db.t", properties={"run": str(random.random())})


class Repeats(Good):
    def datasets(self, config):
        yield Dataset(name="db.t", fields=[Field(path="a"), Field(path="a")])
        yield Dataset(name="db.t", upstreams=[Lineage(upstream="raw_table")])


class Raises(Good):
    def test_connection(self, config):
        raise OSError("no route")


def test_testkit():
    cfg = {"token": "s3cret-token"}
    assert check(Good(), cfg) == {"ok": True, "problems": [], "datasets": 2}
    assert check(Leaky(), cfg)["problems"] == ["a secret from the config appears in the metadata"]
    assert "two runs gave different metadata" in check(Flaky(), cfg)["problems"][0]
    problems = check(Repeats(), cfg)["problems"]
    assert any("names repeat" in p for p in problems) and any("field paths repeat" in p for p in problems)
    assert any("is not a dataset urn" in p for p in problems)
    assert check(Raises(), cfg)["problems"] == ["test_connection raised OSError: no route"]
    assert "does not validate" in check(Good(), {})["problems"][0]


def test_metadata_shape():
    mcps = to_mcps("acme", Good().datasets(Cfg(token="x")))
    aspects = [m["aspectName"] for m in mcps if m["entityUrn"].endswith("db.t0,PROD)")]
    assert aspects == ["datasetProperties", "schemaMetadata", "ownership", "globalTags", "upstreamLineage", "status"]
    with pytest.raises(ValueError):
        Field(path="x", type="decimal")


def test_gallery_and_bridge(monkeypatch):
    monkeypatch.setenv("CAIRN_SOURCE_PLUGINS", os.path.dirname(EXAMPLE))
    monkeypatch.syspath_prepend(os.path.join(EXAMPLE, "src"))
    plugin = next(c for c in all_connectors() if c.id == "plugin-csv-folder")
    assert plugin.source_type == BRIDGE and [f.key for f in plugin.fields] == ["folder", "owner"]
    recipe = build_recipe("plugin-csv-folder", {"folder": os.path.join(EXAMPLE, "tests", "data"), "owner": "ana"})
    assert recipe["source"] == {"type": BRIDGE, "config": {"source": "cairn_csv_folder.source:CsvFolder",
                                                           "config": {"folder": os.path.join(EXAMPLE, "tests", "data"),
                                                                      "owner": "ana"}}}
    assert form_values(recipe)[0] == "plugin-csv-folder"
    mcps = proposals(recipe["source"]["config"])
    assert {m["entityUrn"] for m in mcps} == {"urn:li:dataset:(urn:li:dataPlatform:csv,exports.orders,PROD)",
                                              "urn:li:dataset:(urn:li:dataPlatform:csv,exports.customers,PROD)"}
    assert connection({"source": "cairn_csv_folder.source:CsvFolder", "config": {"folder": "/nope"}})[0] is False
    with pytest.raises(RuntimeError, match="cannot connect"):
        proposals({"source": "cairn_csv_folder.source:CsvFolder", "config": {"folder": "/nope"}})
    sys.modules.pop("cairn_csv_folder.source", None)
