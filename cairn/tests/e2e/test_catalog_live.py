"""Cairn Catalog in the running stack (Compose service cairn-catalog, tables in cairn-store): a table is
created and appended to through the Iceberg REST API, and its metadata and data land in the store."""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

CATALOG = os.environ.get("CAIRN_CATALOG_URL", "http://localhost:8181")
STORE = os.environ.get("CAIRN_STORE_URL", "http://localhost:8333")
TOKEN = os.environ.get("CAIRN_CATALOG_TOKEN", "cairn-catalog-dev-token")
STORE_KEY = (os.environ.get("CAIRN_STORE_ACCESS_KEY", "cairn-store"), os.environ.get("CAIRN_STORE_SECRET_KEY", "cairn-store-dev-secret"))


def test_iceberg_table_through_cairn_catalog_and_store():
    pa = pytest.importorskip("pyarrow")
    pytest.importorskip("pyiceberg")
    boto3 = pytest.importorskip("boto3")
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.io import load_file_io
    from pyiceberg.table import Table

    cat = RestCatalog("cairn", uri=CATALOG, token=TOKEN)
    ns = "e2e_" + uuid.uuid4().hex[:8]
    cat.create_namespace(ns)
    data = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "region": ["EMEA", "AMER", "EMEA"]})
    table = cat.create_table(f"{ns}.orders", schema=data.schema)
    # The catalog tells clients the in-stack store address; from the host, reach it on its mapped port.
    io = load_file_io({"s3.endpoint": STORE, "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                       "s3.region": "us-east-1"}, table.metadata_location)
    table = Table(table.name(), table.metadata, table.metadata_location, io, cat)
    table.append(data)
    reloaded = cat.load_table(f"{ns}.orders")
    assert reloaded.current_snapshot() is not None and "/metadata/00001-" in reloaded.metadata_location
    back = Table(reloaded.name(), reloaded.metadata, reloaded.metadata_location, io, cat).scan().to_arrow()
    assert back.num_rows == 3

    s3 = boto3.client("s3", endpoint_url=STORE, region_name="us-east-1", aws_access_key_id=STORE_KEY[0],
                      aws_secret_access_key=STORE_KEY[1])
    prefix = reloaded.location().removeprefix("s3://lakehouse/") + "/"
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="lakehouse", Prefix=prefix).get("Contents", [])]
    assert any(k.endswith(".metadata.json") for k in keys) and any(k.endswith(".parquet") for k in keys), keys
    cat.drop_table(f"{ns}.orders")
    cat.drop_namespace(ns)
