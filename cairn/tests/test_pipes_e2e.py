"""Pipes (cairn.pipelines.pipes) with real Cairn Stores and Cairn Catalog: files landing in one store are
loaded into Iceberg tables in the lakehouse, as soon as the store's signed event arrives, and by a sweep
for anything an event missed; each file once, failures recorded per file. Needs the store and catalog
binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port, wait  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
SECRET = "pipes-webhook-secret-" + "x" * 16
LANDING_KEY = ("AKLANDING0000000", "landing-secret-" + "y" * 24)


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    import uvicorn
    from fastapi import FastAPI, Request
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.pipelines.pipes import Pipes, build_pipes_router

    d = tmp_path_factory.mktemp("pipes")
    engine = create_engine(f"sqlite:///{d}/cairn.db")
    init_db(engine)
    api = port()
    p = Pipes(engine,
              store={"endpoint": "", "access_key": LANDING_KEY[0], "secret_key": LANDING_KEY[1], "region": "us-east-1"},
              catalog={"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                       "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                       "s3.path-style-access": "true"},
              webhook_secret=SECRET)

    def who(request: Request):
        return Principal("urn:li:corpuser:admin", "token")
    app = FastAPI()
    app.include_router(build_pipes_router(p, who, lambda pr, priv, urn: None))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api, log_level=os.environ.get("PIPES_LOG", "warning")))
    threading.Thread(target=server.run, daemon=True).start()
    wait(api)
    # The landing store, telling Cairn about new objects (signed webhook).
    sp = port()
    targets = d / "targets.json"
    targets.write_text(json.dumps([{"type": "webhook", "id": "cairn-pipes", "url": f"http://127.0.0.1:{api}/v1/pipes/events",
                                    "secret": SECRET}]))
    sdir = tempfile.mkdtemp()
    store = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": LANDING_KEY[0], "CAIRN_STORE_SECRET_KEY": LANDING_KEY[1],
        "CAIRN_STORE_DATA": sdir, "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "landing",
        "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_EVENT_TARGETS_FILE": str(targets)})
    wait(sp)
    p.store["endpoint"] = f"http://127.0.0.1:{sp}"
    import boto3
    s3 = boto3.client("s3", endpoint_url=p.store["endpoint"], aws_access_key_id=LANDING_KEY[0],
                      aws_secret_access_key=LANDING_KEY[1], region_name="us-east-1")
    s3.put_bucket_notification_configuration(Bucket="landing", NotificationConfiguration={"QueueConfigurations": [{
        "Id": "pipes", "QueueArn": "arn:cairn:store:::cairn-pipes", "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "orders/"}]}}}]})
    yield {"pipes": p, "s3": s3, "api": f"http://127.0.0.1:{api}"}
    server.should_exit = True
    store.terminate()
    store.wait(15)
    shutil.rmtree(sdir, ignore_errors=True)


def rows(p, target):
    return p._catalog().load_table(tuple(target.split("."))).scan().to_arrow().num_rows


def wait_for(fn, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def test_files_are_loaded_when_they_land_and_only_once(world):
    import httpx

    p, s3, api = world["pipes"], world["s3"], world["api"]
    r = httpx.post(f"{api}/v1/pipes", json={"name": "orders", "bucket": "landing", "prefix": "orders/", "format": "csv",
                                            "target": "raw.orders", "create_table": True}, timeout=10)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    s3.put_object(Bucket="landing", Key="orders/2026-11-01.csv", Body=b"id,region,amount\n1,EU,9.5\n2,US,3.0\n")
    assert wait_for(lambda: [f for f in p.files(pid) if f["status"] == "ok"]), p.files(pid)   # by the store's event
    assert rows(p, "raw.orders") == 2
    s3.put_object(Bucket="landing", Key="orders/2026-11-02.csv", Body=b"id,region,amount\n3,EU,7.0\n")
    assert wait_for(lambda: rows(p, "raw.orders") == 3)
    # A file that does not fit is recorded as failed; the next one still loads.
    s3.put_object(Bucket="landing", Key="orders/bad.csv", Body=b"nothing,useful\n1,2\n")
    s3.put_object(Bucket="landing", Key="orders/2026-11-03.csv", Body=b"id,region,amount\n4,US,1.0\n")
    assert wait_for(lambda: rows(p, "raw.orders") == 4)
    bad = wait_for(lambda: [f for f in p.files(pid) if f["key"] == "orders/bad.csv"])
    assert bad[0]["status"] == "error" and bad[0]["error"]
    # Each file once: a sweep finds nothing new, and the table is unchanged.
    assert [f for f in p.sweep(pid) if f["status"] == "ok"] == []
    assert rows(p, "raw.orders") == 4
    # The webhook takes only the store's signature.
    body = json.dumps({"Records": []}).encode()
    assert httpx.post(f"{api}/v1/pipes/events", content=body, headers={"x-cairn-signature": "sha256=00"}).status_code == 401


def test_the_sweep_loads_what_no_event_announced(world):
    p, s3 = world["pipes"], world["s3"]
    pipe = p.create({"name": "late", "bucket": "landing", "prefix": "late/", "format": "json", "target": "raw.late",
                     "create_table": True}, "urn:li:corpuser:admin")
    s3.put_object(Bucket="landing", Key="late/a.json", Body=b'{"k": "a", "n": 1}\n{"k": "b", "n": 2}\n')
    s3.put_object(Bucket="landing", Key="late/_SUCCESS", Body=b"")          # markers are not data
    time.sleep(2)
    assert p.files(pipe["id"]) == []                                         # no rule for late/: no event
    done = p.sweep(pipe["id"])
    assert [(f["key"], f["status"], f["rows"]) for f in done] == [("late/a.json", "ok", 2)]
    assert rows(p, "raw.late") == 2 and p.sweep(pipe["id"]) == []
