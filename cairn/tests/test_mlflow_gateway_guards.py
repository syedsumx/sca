"""The MLflow gateway's registry checks against requests MLflow reads differently from how Cairn read them.

The upstream here is a stand-in that parses requests the way MLflow's server does (``mlflow.server.handlers``:
a GET with a query string reads the first value of each parameter; any other request reads its body as JSON
whatever its content type, which MLflow checks only for POST and PUT). The end-to-end test with MLflow's real
server is test_ml_e2e.py."""

import asyncio
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from cairn.ml.mlflow_gateway import MlError, MlflowGateway, ml_models

ANA, MAL = "urn:li:corpuser:ana", "urn:li:corpuser:mallory"


def _p(urn):
    return SimpleNamespace(urn=urn, is_service=False)


class FakeMlflow:
    """Records which registered model each registry call would act on, as MLflow would read it."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not re.fullmatch(r"/[A-Za-z0-9/._-]*", path):     # no MLflow route has such a path
            return httpx.Response(404)
        q = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query)
        if request.method == "GET" and q:
            name = q.get("name", [""])[0]                       # Flask's args.get: the first value
        else:
            try:
                body = json.loads(request.content or b"null")   # get_json(force=True)
                if isinstance(body, str):
                    body = json.loads(body)
            except ValueError:
                body = None
            name = (body or {}).get("name", "") if isinstance(body, dict) else ""
        self.calls.append((request.method, path, name))
        if "mlflow-artifacts/artifacts/" in path:
            return httpx.Response(200, content=b"<script>alert(document.cookie)</script>",
                                  headers={"content-type": "text/html; charset=utf-8",
                                           "content-disposition": "attachment; filename=evil.html",
                                           "x-content-type-options": "nosniff"})
        return httpx.Response(200, json={"registered_model": {"name": name}})

    def acted_on(self, name: str) -> bool:
        return any(n == name for _, _, n in self.calls)


@pytest.fixture()
def gw():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ml_models.create(engine)
    with engine.begin() as conn:
        conn.execute(insert(ml_models).values(name="ana-model", owner=ANA, created_s=0))
        conn.execute(insert(ml_models).values(name="mal-model", owner=MAL, created_s=0))
    fake = FakeMlflow()
    g = MlflowGateway("http://mlflow.invalid", engine, None, None, None, lambda p, priv, urn: False,
                      http=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    g.fake = fake
    return g


def _handle(gw, method, path, query="", body=b"", content_type=""):
    return asyncio.run(gw.handle(_p(MAL), method, path, query, body, content_type))


def _refused_or_untouched(gw, *call):
    try:
        _handle(gw, *call)
    except MlError as e:
        assert e.status in (400, 403, 404)
    assert not gw.fake.acted_on("ana-model"), gw.fake.calls


def test_owner_can_still_change_their_own_model(gw):
    status, _, _ = _handle(gw, "DELETE", "api/2.0/mlflow/registered-models/delete",
                           body=json.dumps({"name": "mal-model"}).encode(), content_type="application/json")
    assert status == 200 and gw.fake.acted_on("mal-model")


def test_a_body_without_a_json_content_type_is_still_read_for_the_check(gw):
    # MLflow parses a DELETE or PATCH body as JSON whatever its content type; Cairn must check that name,
    # not the one in the query string.
    body = json.dumps({"name": "ana-model"}).encode()
    _refused_or_untouched(gw, "DELETE", "api/2.0/mlflow/registered-models/delete", "name=mal-model", body, "")
    _refused_or_untouched(gw, "PATCH", "api/2.0/mlflow/registered-models/update", "name=mal-model", body,
                          "text/plain")
    _refused_or_untouched(gw, "DELETE", "api/2.0/mlflow/model-versions/delete", "name=mal-model", body, "")


def test_a_double_encoded_body_is_read_as_mlflow_reads_it(gw):
    body = json.dumps(json.dumps({"name": "ana-model"})).encode()
    _refused_or_untouched(gw, "DELETE", "api/2.0/mlflow/registered-models/delete", "name=mal-model", body,
                          "application/json")


def test_a_repeated_query_parameter_is_read_as_mlflow_reads_it(gw):
    # Flask reads the first of repeated parameters.
    _refused_or_untouched(gw, "GET", "api/2.0/mlflow/registered-models/get", "name=ana-model&name=mal-model")
    _refused_or_untouched(gw, "GET", "api/2.0/mlflow/model-versions/get-download-uri",
                          "name=ana-model&version=1&name=mal-model")


def test_a_path_cannot_reach_the_registry_around_its_check(gw):
    # The HTTP client removes dot segments before sending, and a decoded "?" or "#" ends the path there:
    # either turns a path the registry check does not recognise into a registry call.
    body = json.dumps({"name": "ana-model"}).encode()
    for path in ("api/2.0/mlflow/runs/../registered-models/delete",
                 "api/2.0/mlflow/./registered-models/delete",
                 "api/2.0/mlflow/registered-models/delete?",
                 "api/2.0/mlflow/registered-models/delete#x"):
        _refused_or_untouched(gw, "DELETE", path, "", body, "application/json")
    _refused_or_untouched(gw, "GET", "api/2.0/mlflow/registered-models/get?name=ana-model")
    # Nor outside the paths Cairn forwards at all.
    try:
        _handle(gw, "GET", "api/2.0/mlflow/../../../static-files/index.html")
    except MlError:
        pass
    assert not any("static-files" in path for _, path, _ in gw.fake.calls), gw.fake.calls


def test_an_artifact_is_downloaded_not_rendered_on_cairns_origin(gw):
    # Anyone may log an artifact; an HTML one opened from Cairn's origin would run as whoever opened the link.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.ml.mlflow_gateway import build_ml_router

    app = FastAPI()
    app.include_router(build_ml_router(gw, lambda: _p(ANA)))
    r = TestClient(app).get("/v1/ml/api/2.0/mlflow-artifacts/artifacts/1/abc/artifacts/evil.html")
    assert r.status_code == 200
    assert r.headers.get("content-disposition", "").startswith("attachment")
    assert "sandbox" in r.headers.get("content-security-policy", "")
    # API answers say the same (an error page or JSON is never a document to render).
    r = TestClient(app).get("/v1/ml/api/2.0/mlflow/registered-models/search")
    assert "sandbox" in r.headers.get("content-security-policy", "")
