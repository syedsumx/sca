"""What one table's or one token's access to the customer's own storage (L132) cannot reach, end to end.

A real Cairn Store with STS stands in for the customer's S3 and for AWS STS, and the real Cairn Catalog loads its
external locations from a file (its own tokens: an administrator's, and a team's kept to namespace ``team``):

* a table in an external location cannot take another table's place, nor one around or inside it, so the access
  handed out with it never reaches another table's files;
* an S3 path IAM would read as a pattern (``*``, ``?``, ``$``) is refused, so a session policy is never widened;
* a token kept to some namespaces gets no access to files by path (those belong to no namespace);
* a view's metadata, which the catalog writes itself, is not written into a read-only location.

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
STS = "ext-sts-secret-" + "s" * 32
SCHEMA = {"type": "struct", "schema-id": 0, "fields": [{"id": 1, "name": "id", "required": False, "type": "long"}]}


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

    d = tempfile.mkdtemp(prefix="ext-access-")
    procs: list = []
    try:
        sp, cp = _port(), _port()
        procs.append(subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
            "CAIRN_STORE_DATA": f"{d}/s", "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse,customer-s3",
            "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_STS_SECRET": STS}))
        _wait(sp)
        store = f"http://127.0.0.1:{sp}"
        kw = {"endpoint_url": store, "aws_access_key_id": STORE_KEY[0], "aws_secret_access_key": STORE_KEY[1],
              "region_name": "us-east-1"}
        me = boto3.client("sts", **kw).get_caller_identity()["Arn"]
        iam = boto3.client("iam", **kw)
        iam.create_role(RoleName="customer-tables", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": me}, "Action": "sts:AssumeRole"}]}))
        iam.put_role_policy(RoleName="customer-tables", PolicyName="customer", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*",
                                                    "Resource": ["arn:aws:s3:::customer-s3", "arn:aws:s3:::customer-s3/*"]}]}))
        role_arn = iam.get_role(RoleName="customer-tables")["Role"]["Arn"]
        secret = f"{d}/customer-s3-secret"                 # the credential's secret: a file the catalog reads
        with open(secret, "w") as fh:
            fh.write(STORE_KEY[1])
        with open(f"{d}/locations.json", "w") as fh:
            json.dump({"credentials": [{"name": "customer-s3", "provider": "s3", "secret_ref": f"file:{secret}",
                                        "config": {"role_arn": role_arn, "endpoint": store, "sts_endpoint": store,
                                                   "access_key_id": STORE_KEY[0]}}],
                       "locations": [{"name": "tables", "url": "s3://customer-s3/tables", "credential": "customer-s3"},
                                     {"name": "archive", "url": "s3://customer-s3/archive", "credential": "customer-s3",
                                      "read_only": True}]}, fh)
        with open(f"{d}/tokens.json", "w") as fh:
            json.dump([{"token": TEAM, "name": "team", "namespaces": ["team"]}], fh)
        log = open(f"{d}/catalog.log", "w")
        procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=log, stderr=subprocess.STDOUT, env={
            **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": ADMIN,
            "CAIRN_CATALOG_TOKENS_FILE": f"{d}/tokens.json", "CAIRN_CATALOG_S3_ENDPOINT": store,
            "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0], "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1],
            "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse", "CAIRN_CATALOG_FSYNC": "false",
            "CAIRN_CATALOG_LOCATIONS_FILE": f"{d}/locations.json"}))
        _wait(cp)
        catalog = f"http://127.0.0.1:{cp}"

        def rest(method, path, token=ADMIN, body=None):
            return httpx.request(method, f"{catalog}/v1/{path}", json=body, headers={"Authorization": f"Bearer {token}"},
                                 timeout=30)
        for ns, loc in (("sales", "s3://customer-s3/tables/sales"), ("team", "s3://customer-s3/tables/team")):
            r = rest("POST", "namespaces", body={"namespace": [ns], "properties": {"location": loc}})
            assert r.status_code == 200, (r.status_code, r.text, open(f"{d}/catalog.log").read()[-2000:])
        # The sales team's table, with a data file the team token must never reach.
        r = rest("POST", "namespaces/sales/tables", body={"name": "orders", "schema": SCHEMA})
        assert r.status_code == 200, r.text
        orders = r.json()["metadata"]["location"]
        key = orders.removeprefix("s3://customer-s3/") + "/data/part-0.parquet"
        boto3.client("s3", **kw).put_object(Bucket="customer-s3", Key=key, Body=b"sales secrets")
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
        s3.get_object(Bucket="customer-s3", Key=key)
        return True
    except ClientError:
        return False


def test_a_table_cannot_take_another_tables_place_in_an_external_location(lake):
    rest, orders = lake["rest"], lake["orders"]
    for i, loc in enumerate([orders, f"{orders}/", "s3://customer-s3/tables/sales", f"{orders}/data",
                             "s3a://customer-s3/tables/sales//"]):
        r = rest("POST", "namespaces/team/tables", TEAM, {"name": f"grab{i}", "schema": SCHEMA, "location": loc})
        assert r.status_code == 409 and "overlaps the location of table sales.orders" in r.text, (loc, r.status_code, r.text)
        assert "storage-credentials" not in r.text
    # Nor by registering the other table's metadata, nor by moving a table there.
    md = rest("GET", "namespaces/sales/tables/orders").json()["metadata-location"]
    r = rest("POST", "namespaces/team/register", TEAM, {"name": "grab-reg", "metadata-location": md})
    assert r.status_code == 409, r.text
    r = rest("POST", "namespaces/team/tables", TEAM, {"name": "own", "schema": SCHEMA,
                                                      "location": "s3://customer-s3/tables/team/own"})
    assert r.status_code == 200, r.text
    # Its own place: the access handed out with it reads there, and not the sales table's files.
    cfg = r.json()["config"]
    assert not _reads(lake, cfg, lake["key"])
    r = rest("POST", "namespaces/team/tables/own", TEAM, {"requirements": [], "updates": [
        {"action": "set-location", "location": orders}]})
    assert r.status_code == 409, r.text
    # A sibling whose name only starts like the other's is its own place.
    r = rest("POST", "namespaces/team/tables", TEAM, {"name": "sibling", "schema": SCHEMA, "location": f"{orders}x"})
    assert r.status_code == 200, r.text
    assert not _reads(lake, r.json()["config"], lake["key"])


def test_an_s3_path_iam_would_read_as_a_pattern_is_refused(lake):
    rest, orders = lake["rest"], lake["orders"]
    stem = orders.rsplit("-", 1)[0]                        # s3://customer-s3/tables/sales/orders
    for loc in (f"{stem}*", f"{stem}-????????", "s3://customer-s3/tables/sales/${aws:username}"):
        r = rest("POST", "namespaces/team/tables", TEAM, {"name": f"w{uuid.uuid4().hex[:6]}", "schema": SCHEMA, "location": loc})
        assert r.status_code == 400 and "may not contain" in r.text, (loc, r.status_code, r.text)
    # Nor files by such a path.
    r = rest("GET", f"locations/credentials?url={stem}*&access=read")
    assert r.status_code == 400, r.text


def test_a_token_kept_to_namespaces_gets_no_files_by_path(lake):
    rest = lake["rest"]
    for access in ("read", "write"):
        r = rest("GET", f"locations/credentials?url=s3://customer-s3/tables/sales&access={access}", TEAM)
        assert r.status_code == 403 and "kept to some namespaces" in r.text, (access, r.status_code, r.text)
    # The administrator's token still does (the catalog's own, unrestricted), for that path only.
    r = rest("GET", "locations/credentials?url=s3://customer-s3/tables/sales&access=read")
    assert r.status_code == 200, r.text
    assert _reads(lake, r.json()["config"], lake["key"])


def test_a_view_is_not_written_into_a_read_only_location(lake):
    import boto3

    rest = lake["rest"]
    view = {"name": "v1", "location": "s3://customer-s3/archive/v1", "schema": SCHEMA, "properties": {}, "view-version": {
        "version-id": 1, "timestamp-ms": int(time.time() * 1000), "schema-id": 0, "summary": {"operation": "create"},
        "default-namespace": ["sales"], "representations": [{"type": "sql", "sql": "SELECT 1 AS id", "dialect": "trino"}]}}
    r = rest("POST", "namespaces/sales/views", body=view)
    assert r.status_code == 403 and "read-only" in r.text, (r.status_code, r.text)
    listed = boto3.client("s3", **lake["s3kw"]).list_objects_v2(Bucket="customer-s3", Prefix="archive/")
    assert listed.get("KeyCount", 0) == 0, listed
    # In a writable location the view is created as before.
    r = rest("POST", "namespaces/sales/views", body={**view, "location": "s3://customer-s3/tables/sales/v1"})
    assert r.status_code == 200, r.text
