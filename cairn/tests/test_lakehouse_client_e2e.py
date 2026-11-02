"""pyiceberg and DuckDB as Cairn identities (cairn_lakehouse), on Cairn Catalog with the store's STS:
governed per table, like Spark. Needs the store and catalog binaries."""

import importlib.util
import os
import sys

import pytest

from test_spark_e2e import ANA, BOB, lake, people  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
SRC = os.path.join(os.path.dirname(__file__), "..", "lakehouse-client", "src")


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, SRC)
    import cairn_lakehouse
    yield cairn_lakehouse
    sys.path.remove(SRC)


def test_pyiceberg_and_duckdb_are_governed_like_spark(cairn, lake, client, monkeypatch):  # noqa: F811
    from pyiceberg.exceptions import ForbiddenError
    import pyarrow as pa

    url, s = cairn
    s.lakehouse.catalog_uris = (lake["catalog"],)
    monkeypatch.setenv("CAIRN_URL", url)
    monkeypatch.setenv("CAIRN_CATALOG_URI", lake["catalog"])
    monkeypatch.setenv("CAIRN_TOKEN", ANA)
    cat = client.catalog()
    assert [t[1] for t in cat.list_tables("sales")] == ["orders"] and cat.list_tables("hr") == []
    assert cat.load_table("analytics.products").scan().to_arrow().num_rows == 3
    with pytest.raises(ForbiddenError, match="Masks or row filters apply"):
        cat.load_table("sales.orders")  # masked for ana: never handed to a file reader
    t = cat.create_table("analytics.from_pyiceberg", schema=pa.schema([("sku", pa.string())]))
    t.append(pa.table({"sku": ["x", "y"]}))
    assert lake["admin"].load_table("analytics.from_pyiceberg").scan().to_arrow().num_rows == 2
    # DuckDB over the same governed tables, through pyiceberg: no extension, no store key.
    con = client.duckdb_connection(["analytics.products"], cat=cat)
    assert con.sql("SELECT count(*), max(price) FROM products").fetchall() == [(3, 9)]
    # bob sees no analytics at all.
    monkeypatch.setenv("CAIRN_TOKEN", BOB)
    with pytest.raises(ForbiddenError):
        client.catalog().load_table("analytics.products")
    assert importlib.util.find_spec("duckdb")
