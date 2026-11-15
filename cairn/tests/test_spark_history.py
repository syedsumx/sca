"""The Spark history server behind Cairn: a person reaches only their own applications, whatever the
path's encoding (a decoded ``%2e%2e`` or ``%3F`` must not slip past the ownership check)."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cairn.dataaccess.spark_history import SparkHistory, build_spark_history_router


@pytest.fixture
def client():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path.endswith("/environment"):
            owner = "ana" if "app-ana" in req.url.path else "bob"
            return httpx.Response(200, json={"sparkProperties": [["spark.cairn.user", owner]]})
        if req.url.path == "/api/v1/applications":
            return httpx.Response(200, json=[{"id": "app-ana"}, {"id": "app-bob"}])
        return httpx.Response(200, text="page")

    history = SparkHistory("http://hs", http=httpx.Client(transport=httpx.MockTransport(handler)))
    app = FastAPI()
    ana = SimpleNamespace(is_service=False, via="", urn="urn:li:corpuser:ana")
    app.include_router(build_spark_history_router(history, lambda: ana, lambda p: False))
    return TestClient(app), seen


def test_own_applications_only(client):
    c, _ = client
    assert c.get("/v1/spark/history/api/v1/applications").json() == [{"id": "app-ana"}]
    assert c.get("/v1/spark/history/api/v1/applications/app-ana/jobs").status_code == 200
    assert c.get("/v1/spark/history/history/app-bob/jobs/").status_code == 404


@pytest.mark.parametrize("path", ["api/v1/x/%2e%2e/applications/app-bob/jobs", "history/%2e/app-bob/jobs/",
                                  "api/v1/applications%3Fstatus=completed", "api/v1//applications/app-bob/jobs",
                                  "api/v1/applications/app-bob%23"])
def test_paths_that_dodge_the_ownership_check_are_refused(client, path):
    c, seen = client
    assert c.get(f"/v1/spark/history/{path}").status_code == 404
    assert not any("app-bob" in s or s == "/api/v1/applications" for s in seen)
