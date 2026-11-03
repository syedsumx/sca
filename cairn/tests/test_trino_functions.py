"""Stored functions as catalog assets: Trino's statements as Trino receives them, read into the catalog
(against real Trino in tests/test_trino_e2e.py)."""

import json

import pytest

from cairn.common.core import InMemoryGraph
from cairn.common.db import get_engine, init_db
from cairn.dataaccess.trino_functions import FunctionCatalog, parse_create, parse_drop, types_of

PY = ("CREATE OR REPLACE FUNCTION routines.shared.shout(x varchar) RETURNS varchar LANGUAGE PYTHON "
      "WITH (handler = 'shout') COMMENT 'Shouts' AS $$\ndef shout(x):\n    return x.upper()\n$$")


def event(sql, user="admin", state="FINISHED", failed=False, catalog="", schema=""):
    return {"metadata": {"queryId": "q", "queryState": state, "query": sql},
            "context": {"user": user, "catalog": catalog, "schema": schema}, **({"failureInfo": {"errorCode": "X"}} if failed else {})}


@pytest.fixture
def fc(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    return FunctionCatalog(engine, InMemoryGraph(), {"routines"})


def test_statements_are_read_as_trino_has_them():
    f = parse_create("CREATE FUNCTION routines.shared.area(w decimal(10, 2), h double) RETURNS decimal(20,2) "
                     "COMMENT 'It''s an area' RETURN w * h")
    assert f["parts"] == ["routines", "shared", "area"] and f["types"] == "decimal(10, 2), double"
    assert (f["returns"], f["language"], f["comment"], f["body"]) == ("decimal(20,2)", "SQL", "It's an area", "RETURN w * h")
    p = parse_create(PY)
    assert p["replace"] and p["language"] == "PYTHON" and p["body"].startswith("def shout") and p["comment"] == "Shouts"
    assert parse_create('create function "Routines".s."Odd Name"(x bigint) returns bigint return x')["parts"] == \
        ["Routines", "s", "Odd Name"]
    assert parse_drop("DROP FUNCTION IF EXISTS routines.shared.area(decimal(10,2), double)") == \
        (["routines", "shared", "area"], "decimal(10,2), double")
    assert parse_create("SELECT 1") is None and parse_drop("DROP TABLE x") is None
    assert types_of("varchar(20), integer", named=False) == "varchar(20), integer"


def test_events_publish_replace_overload_and_drop(fc):
    urn = fc.asset_urn("routines", "shared", "area")
    fc.from_event(event("CREATE FUNCTION routines.shared.area(w double, h double) RETURNS double COMMENT 'Area' RETURN w * h",
                        user="kim"))
    info = fc.graph.get_aspect(urn, "dataJobInfo")
    props = info["customProperties"]
    assert info["name"] == "routines.shared.area" and info["description"] == "Area"
    assert props["function_urn"] == "urn:li:function:routines.shared.area" and props["body"] == "RETURN w * h"
    assert fc.graph.get_aspect(urn, "subTypes") == {"typeNames": ["Function"]}
    assert [o["owner"] for o in fc.graph.get_aspect(urn, "ownership")["owners"]] == ["urn:li:corpuser:kim"]
    # Replaced by someone else: still Kim's. An overload is added beside it.
    fc.from_event(event("CREATE OR REPLACE FUNCTION routines.shared.area(w double, h double) RETURNS double RETURN w * h * 1.0",
                        user="admin"))
    fc.from_event(event("CREATE FUNCTION area(s double) RETURNS double RETURN s * s", catalog="routines", schema="shared"))
    sigs = json.loads(fc.graph.get_aspect(urn, "dataJobInfo")["customProperties"]["signatures"])
    assert [s["types"] for s in sigs] == ["double, double", "double"]
    assert [o["owner"] for o in fc.graph.get_aspect(urn, "ownership")["owners"]] == ["urn:li:corpuser:kim"]
    # Failed statements and other catalogs change nothing.
    fc.from_event(event("DROP FUNCTION routines.shared.area(double)", failed=True))
    fc.from_event(event("CREATE FUNCTION lake.s.f(x bigint) RETURNS bigint RETURN x"))
    assert len(json.loads(fc.graph.get_aspect(urn, "dataJobInfo")["customProperties"]["signatures"])) == 2
    assert fc.graph.get_aspect(fc.asset_urn("lake", "s", "f"), "dataJobInfo") is None
    # Dropping one overload keeps the other; dropping the last removes the asset.
    fc.from_event(event("DROP FUNCTION routines.shared.area(double)"))
    assert [s["types"] for s in json.loads(fc.graph.get_aspect(urn, "dataJobInfo")["customProperties"]["signatures"])] == \
        ["double, double"]
    fc.from_event(event("DROP FUNCTION routines.shared.area(double, double)"))
    assert fc.graph.get_aspect(urn, "status") == {"removed": True}


def test_sync_adds_what_was_missed_and_removes_what_is_gone(fc):
    fc.from_event(event("CREATE FUNCTION routines.shared.gone(x bigint) RETURNS bigint RETURN x"))
    fc.from_event(event(PY))
    listing = {'SHOW SCHEMAS FROM "routines"': [["information_schema"], ["shared"]],
               'SHOW FUNCTIONS FROM "routines"."shared"': [
                   ["shout", "varchar", "varchar", "scalar", True, "Shouts"],
                   ["missed", "bigint", "bigint, bigint", "scalar", True, ""]]}
    out = fc.sync(lambda sql: listing[sql])
    assert out == {"functions": 2, "added": 1, "removed": 1}
    assert fc.graph.get_aspect(fc.asset_urn("routines", "shared", "gone"), "status") == {"removed": True}
    missed = fc.graph.get_aspect(fc.asset_urn("routines", "shared", "missed"), "dataJobInfo")["customProperties"]
    assert json.loads(missed["signatures"])[0]["types"] == "bigint, bigint"
    # What Cairn learned from the event (the body, the language) is kept when Trino's listing agrees.
    shout = fc.graph.get_aspect(fc.asset_urn("routines", "shared", "shout"), "dataJobInfo")["customProperties"]
    assert shout["language"] == "PYTHON" and shout["body"].startswith("def shout")
    assert fc.sync(lambda sql: listing[sql]) == {"functions": 2, "added": 0, "removed": 0}
