"""The Spark history server behind Cairn's sign-in: each person reaches only their own applications.

A stand-in history server (httpx's MockTransport, which sees each request as httpx would send it) has
two applications, ``app-ana`` and ``app-bob``. The real history server is exercised in
``tests/test_spark_e2e.py``; here the proxy's path handling is checked: a path that the HTTP client or
the history server would resolve to another application, or to the full listing, must not get past the
owner check.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cairn.common.security import Principal
from cairn.dataaccess.spark_history import SparkHistory, build_spark_history_router

OWNERS = {"app-ana": "ana", "app-bob": "bob"}


def _history(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.rstrip("/") == "/api/v1/applications":
        return httpx.Response(200, json=[{"id": a, "name": a} for a in OWNERS])
    for app_id, user in OWNERS.items():
        if path == f"/api/v1/applications/{app_id}/environment":
            return httpx.Response(200, json={"sparkProperties": [["spark.cairn.user", user]]})
        if path.startswith((f"/api/v1/applications/{app_id}", f"/history/{app_id}")):
            return httpx.Response(200, json={"app": app_id})
    return httpx.Response(404, text="not found")


@pytest.fixture()
def get():
    history = SparkHistory("http://history:18080", http=httpx.Client(transport=httpx.MockTransport(_history)))
    app = FastAPI()

    def who(request: Request):
        return Principal(request.headers["x-who"], "token")

    app.include_router(build_spark_history_router(history, who, lambda p: p.urn == "urn:li:corpuser:admin"))
    c = TestClient(app)
    return lambda path, u: c.get(f"/v1/spark/history/{path}", headers={"x-who": f"urn:li:corpuser:{u}"})


def test_each_person_sees_their_own_applications(get):
    assert [a["id"] for a in get("api/v1/applications", "ana").json()] == ["app-ana"]
    assert [a["id"] for a in get("api/v1/applications/", "ana").json()] == ["app-ana"]
    assert get("api/v1/applications/app-ana/jobs", "ana").json() == {"app": "app-ana"}
    assert get("api/v1/applications/app-bob/jobs", "ana").status_code == 404
    assert get("history/app-bob/jobs/", "ana").status_code == 404
    assert [a["id"] for a in get("api/v1/applications", "admin").json()] == ["app-ana", "app-bob"]
    assert get("history/app-bob/jobs/", "admin").status_code == 200
    assert get("static/webui.css", "ana").status_code == 404  # passed on (the stand-in has none)


@pytest.mark.parametrize("path", [
    "static/%2e%2e/api/v1/applications/app-bob/logs",          # ../ after an unchecked prefix
    "x/%2e%2e/history/app-bob/jobs/",
    "api/v1/%2e/applications/app-bob/jobs",                   # ./ in the middle
    "api//v1/applications/app-bob/jobs",                      # an empty segment
    "api;x/v1/applications/app-bob/jobs",                     # Jetty drops ;parameters
    "api/v1/applications%3Fx/../app-bob/jobs",
])
def test_paths_that_resolve_to_another_application_are_refused(get, path):
    r = get(path, "ana")
    assert r.status_code in (400, 404), (path, r.status_code, r.text)
    assert "app-bob" not in r.text


@pytest.mark.parametrize("path", [
    "api/v1/applications%3F",                                 # ? : the listing, with the rest as a query
    "api/v1/applications%23",                                 # # : the rest as a fragment
    "api/v1/applications/.",
    "api/v1/./applications",
    "api/v1/applications/x/%2e%2e",
])
def test_paths_that_resolve_to_the_listing_are_filtered_or_refused(get, path):
    r = get(path, "ana")
    assert r.status_code in (200, 400, 404), (path, r.status_code)
    if r.status_code == 200:
        assert [a["id"] for a in json.loads(r.content)] == ["app-ana"], path
    assert "app-bob" not in r.text
