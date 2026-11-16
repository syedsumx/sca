"""What one table's access to Cairn Store cannot reach, end to end (the Store side of the L132 review).

The catalog vends keys for a table in Cairn Store with an STS session policy on the table's location. With Manage on
a namespace (a catalog token kept to namespace ``team``), a table could be created, registered or moved to the
warehouse root, to another table's location, or around or inside it, and its keys then read (and wrote) the other
table's files; and a path IAM reads as a pattern (``*``, ``?``, ``${...}``) widened the policy. Here a real Cairn
Store with STS and the real Cairn Catalog (scoped vending, no residency, no external locations) show:

* a table cannot take the warehouse, a namespace's place, another table's place, nor one around or inside it (409);
* a Cairn Store path with ``*``, ``?`` or ``$`` is refused (400);
* a view cannot sit in a table's files, nor a table around a view;
* a table at its default location, or one beside another whose name only starts the same, works as before, and its
  keys reach its own files only;
* a second table on a table's files (registering its metadata again) is refused.

Needs the store and catalog binaries (no docker)."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(not all(os.environ.get(v) for v in ("CAIRN_STORE_BIN", "CAIRN_CATALOG_BIN")),
                                reason="needs CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

STORE_KEY = ("cairn-catalog", "catalog-store-secret")
ADMIN = "admin-" + "a" * 24
TEAM = "team-only-" + "t" * 24
STS = "store-sts-secret-" + "s" * 32
SCHEMA = {"type": "struct", "schema-id": 0, "fields": [{"id": 1, "name": "id", "required": False, "type": "long"}]}
WAREHOUSE = "s3://lakehouse/warehouse"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(p: int, seconds: float = 30) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(p)


@pytest.fixture(scope="module")
def lake():
    import boto3

    d = tempfile.mkdtemp(prefix="store-place-")
    procs: list = []
    try:
        sp, cp = _port(), _port()
        procs.append(subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
            "CAIRN_STORE_DATA": f"{d}/s", "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse",
            "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_STS_SECRET": STS}))
        _wait(sp)
        store = f"http://127.0.0.1:{sp}"
        kw = {"endpoint_url": store, "aws_access_key_id": STORE_KEY[0], "aws_secret_access_key": STORE_KEY[1],
              "region_name": "us-east-1"}
        with open(f"{d}/tokens.json", "w") as fh:
            json.dump([{"token": TEAM, "name": "team", "namespaces": ["team"]}], fh)
        log = open(f"{d}/catalog.log", "w")
        procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=log, stderr=subprocess.STDOUT, env={
            **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": ADMIN,
            "CAIRN_CATALOG_TOKENS_FILE": f"{d}/tokens.json", "CAIRN_CATALOG_S3_ENDPOINT": store,
            "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0], "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1],
            "CAIRN_CATALOG_WAREHOUSE": WAREHOUSE, "CAIRN_CATALOG_FSYNC": "false",
            "CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables"}))
        _wait(cp)
        catalog = f"http://127.0.0.1:{cp}"

        def rest(method, path, token=ADMIN, body=None):
            return httpx.request(method, f"{catalog}/v1/{path}", json=body, headers={"Authorization": f"Bearer {token}"},
                                 timeout=30)
        for ns in ("sales", "team"):
            r = rest("POST", "namespaces", body={"namespace": [ns]})
            assert r.status_code == 200, (r.status_code, r.text, open(f"{d}/catalog.log").read()[-2000:])
        # The sales team's table, at its default location, with a data file the team token must never reach.
        r = rest("POST", "namespaces/sales/tables", body={"name": "orders", "schema": SCHEMA})
        assert r.status_code == 200, r.text
        orders = r.json()["metadata"]["location"]
        assert orders.startswith(f"{WAREHOUSE}/sales/orders-"), orders
        key = orders.removeprefix("s3://lakehouse/") + "/data/part-0.parquet"
        boto3.client("s3", **kw).put_object(Bucket="lakehouse", Key=key, Body=b"sales secrets")
        yield {"rest": rest, "orders": orders, "key": key, "s3kw": kw, "dir": d}
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait(15)
        shutil.rmtree(d, ignore_errors=True)


def _reads(lake, cfg: dict, key: str) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", endpoint_url=lake["s3kw"]["endpoint_url"], region_name="us-east-1",
                      aws_access_key_id=cfg["s3.access-key-id"], aws_secret_access_key=cfg["s3.secret-access-key"],
                      aws_session_token=cfg["s3.session-token"])
    try:
        s3.get_object(Bucket="lakehouse", Key=key)
        return True
    except ClientError:
        return False


def test_a_store_table_cannot_take_the_warehouse_or_another_tables_place(lake):
    rest, orders = lake["rest"], lake["orders"]
    for i, loc in enumerate([WAREHOUSE, f"{WAREHOUSE}/", f"{WAREHOUSE}/sales", orders, f"{orders}/", f"{orders}/data",
                             "s3a://lakehouse/warehouse/sales//", "s3://lakehouse/warehouse/team"]):
        r = rest("POST", "namespaces/team/tables", TEAM, {"name": f"grab{i}", "schema": SCHEMA, "location": loc})
        leak = r.status_code == 200 and _reads(lake, r.json()["config"], lake["key"])
        assert r.status_code == 409, (loc, r.status_code, "its keys read the sales table's file" if leak else r.text[:300])
        assert "storage-credentials" not in r.text and "s3.session-token" not in r.text, loc
    # Nor by registering the other table's metadata, nor by moving a table there.
    md = rest("GET", "namespaces/sales/tables/orders").json()["metadata-location"]
    r = rest("POST", "namespaces/team/register", TEAM, {"name": "grab-reg", "metadata-location": md})
    assert r.status_code == 409, r.text
    # A table at its default location: created as before, and its keys read its own files, not the sales table's.
    r = rest("POST", "namespaces/team/tables", TEAM, {"name": "own", "schema": SCHEMA})
    assert r.status_code == 200, r.text
    own = r.json()
    assert own["metadata"]["location"].startswith(f"{WAREHOUSE}/team/own-")
    assert not _reads(lake, own["config"], lake["key"])
    for loc in (orders, WAREHOUSE, f"{WAREHOUSE}/sales"):
        r = rest("POST", "namespaces/team/tables/own", TEAM, {"requirements": [], "updates": [
            {"action": "set-location", "location": loc}]})
        assert r.status_code == 409, (loc, r.status_code, r.text)
    # A data-only commit on it (its location unchanged) still works.
    r = rest("POST", "namespaces/team/tables/own", TEAM, {"requirements": [], "updates": [
        {"action": "set-properties", "updates": {"owner": "team"}}]})
    assert r.status_code == 200, r.text
    # A sibling whose name only starts like the other's is its own place.
    r = rest("POST", "namespaces/team/tables", TEAM, {"name": "sibling", "schema": SCHEMA, "location": f"{orders}x"})
    assert r.status_code == 200, r.text
    assert not _reads(lake, r.json()["config"], lake["key"])


def test_a_store_path_iam_would_read_as_a_pattern_is_refused(lake):
    rest, orders = lake["rest"], lake["orders"]
    stem = orders.rsplit("-", 1)[0]                        # s3://lakehouse/warehouse/sales/orders
    for loc in (f"{stem}*", f"{stem}-????????", f"{WAREHOUSE}/sales/${{aws:username}}", f"{WAREHOUSE}/*"):
        r = rest("POST", "namespaces/team/tables", TEAM, {"name": f"w{uuid.uuid4().hex[:6]}", "schema": SCHEMA, "location": loc})
        leak = r.status_code == 200 and _reads(lake, r.json()["config"], lake["key"])
        assert r.status_code == 400 and "may not contain" in r.text, (
            loc, r.status_code, "its keys read the sales table's file" if leak else r.text[:300])


def _view(name: str, location: str | None = None) -> dict:
    v = {"name": name, "schema": SCHEMA, "properties": {}, "view-version": {
        "version-id": 1, "timestamp-ms": int(time.time() * 1000), "schema-id": 0, "summary": {"operation": "create"},
        "default-namespace": ["team"], "representations": [{"type": "sql", "sql": "SELECT 1 AS id", "dialect": "trino"}]}}
    if location:
        v["location"] = location
    return v


def test_views_and_tables_do_not_share_files(lake):
    rest, orders = lake["rest"], lake["orders"]
    # A view whose metadata would be written among the sales table's files (which its writers' keys reach).
    r = rest("POST", "namespaces/team/views", TEAM, _view("in-orders", f"{orders}/v"))
    assert r.status_code == 409, r.text[:300]
    # A view at its default location, then a table around it.
    r = rest("POST", "namespaces/team/views", TEAM, _view("v1"))
    assert r.status_code == 200, r.text
    vloc = r.json()["metadata"]["location"]
    r = rest("POST", "namespaces/team/tables", TEAM, {"name": "around-view", "schema": SCHEMA, "location": vloc})
    assert r.status_code == 409, r.text


def test_a_table_at_its_own_location_keeps_its_keys_and_nothing_goes_inside(lake):
    """A table at a location of its own (not its default) works as before; nothing new is placed inside it. (A table
    already placed around another by an earlier catalog keeps its keys too: the catalog's unit tests build that state,
    which this catalog no longer lets anyone make.)"""
    import boto3

    rest = lake["rest"]
    loc = f"{WAREHOUSE}/sales/legacy-wide"
    r = rest("POST", "namespaces/sales/tables", body={"name": "legacy", "schema": SCHEMA, "location": loc})
    assert r.status_code == 200, r.text
    legacy_md = r.json()["metadata-location"]
    s3 = boto3.client("s3", **lake["s3kw"])
    s3.put_object(Bucket="lakehouse", Key="warehouse/sales/legacy-wide/data/x.parquet", Body=b"legacy rows")
    # Readable as before, by the administrator: the keys still reach the table's own files.
    r = rest("GET", "namespaces/sales/tables/legacy")
    assert r.status_code == 200, r.text
    assert _reads(lake, r.json()["config"], "warehouse/sales/legacy-wide/data/x.parquet")
    # A new table inside it is refused, by default location or by name.
    r = rest("POST", "namespaces/sales/tables", body={"name": "inner", "schema": SCHEMA, "location": f"{loc}/inner"})
    assert r.status_code == 409 and "sales.legacy" in r.text, (r.status_code, r.text[:300])
    # Registering the same metadata under a second name (a second table on the same files) is refused too.
    r = rest("POST", "namespaces/sales/register", body={"name": "legacy2", "metadata-location": legacy_md})
    assert r.status_code == 409, r.text
