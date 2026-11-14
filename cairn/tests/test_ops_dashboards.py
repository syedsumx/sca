"""Ops dashboards (L143) without the observability stack: what protects the process and the backends, and what a
malformed input becomes. The backends are real HTTP servers in this process that answer as Prometheus, Loki and Tempo
do and record what Cairn asked (tests/test_ops_dashboards_e2e.py runs the same paths against the real images).

* alert label patterns are not regular expressions: a pathological one returns at once, the supported forms match,
  and regex syntax is refused (422) when the tile is saved or previewed;
* the minimum auto-refresh (CAIRN_OPS_MIN_REFRESH) is enforced on save and preview (422) and on Grafana import
  (raised to it), and every render says it so the page never polls faster;
* every backend call carries the backend's own timeout where its API has one (Prometheus), the client's timeout,
  and a cap on the answer's size, read as it arrives; range selectors inside expressions are bounded by the range limit;
* malformed Grafana JSON is a 422 naming the field, and the import's body is at most 2 MB;
* one tile's unexpected error (a KeyError, a ValidationError) shows in that tile while the others render."""

from __future__ import annotations

import http.server
import json
import threading
import time
import types
import urllib.parse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.common.security import Principal
from cairn.observability.backends import Backends
from cairn.observability.dashboards import (OpsError, OpsTiles, _matches, build_ops_router, label_pattern,
                                            validate_control, validate_tile)
from cairn.observability.grafana import from_grafana
from cairn.sqlworkspace.boards import Boards, build_boards_router
from cairn.sqlworkspace.workspace import WorkspaceError

ADMIN = "urn:li:corpuser:admin"


# ---- backends that answer as Prometheus, Loki and Tempo do, and record the requests -------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.server.seen.append((u.path, q))
        big = self.server.big
        if big:
            body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"pad": "x" * 1000}, "values": [[1, "1"]]}] * big}}).encode()
            self.send_response(200)
            if self.server.chunked:                     # no Content-Length: the size is found while reading
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i in range(0, len(body), 4096):
                    part = body[i:i + 4096]
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/v1/query" or (u.path == "/loki/api/v1/query" and "time" in q):
            doc = {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"service": "a"}, "value": [time.time(), "1"]}]}}
        elif u.path == "/loki/api/v1/query_range" and "limit" in q:
            doc = {"status": "success", "data": {"resultType": "streams", "result": [
                {"stream": {"service_name": "a"}, "values": [[str(time.time_ns()), "a line"]]}]}}
        elif u.path in ("/api/v1/query_range", "/loki/api/v1/query_range"):
            doc = {"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": "a"}, "values": [[time.time(), "1"]]}]}}
        elif u.path == "/api/search":
            doc = {"traces": [{"traceID": "ab" * 16, "rootServiceName": "a", "rootTraceName": "GET /",
                               "startTimeUnixNano": str(time.time_ns()), "durationMs": 3}]}
        else:
            doc = {"status": "success", "data": []}
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def backend():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen, srv.big, srv.chunked = [], 0, False
    srv.handle_error = lambda request, address: None        # a client that stopped reading (the size cap)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _ops(url: str, env: dict | None = None, alerts=None) -> OpsTiles:
    backends = Backends({"CAIRN_OBS_PROMETHEUS_URL": url, "CAIRN_OBS_LOKI_URL": url, "CAIRN_OBS_TEMPO_URL": url})
    obs = types.SimpleNamespace(backends=backends, _prom=lambda: backends.prometheus,
                                alerts=alerts or (lambda state, limit: []))
    return OpsTiles(obs, lambda actor, privilege: True, env=env or {})


@pytest.fixture
def boards(tmp_path, backend):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    b = Boards(engine, None)
    b.ops = _ops(f"http://127.0.0.1:{backend.server_address[1]}")
    return b


def _client(router) -> TestClient:
    app = FastAPI()

    def who(request: Request) -> Principal:
        return Principal(request.headers.get("X-Cairn-Actor", ADMIN), "user")

    app.include_router(router(who))
    return TestClient(app)


# ---- 1. alert label patterns ----------------------------------------------------------------------------------------
def test_alert_label_patterns_are_linear_and_refuse_regex_syntax():
    evil = ".*" * 16 + "@"
    for have in ("a" * 200, "a" * 20_000, "@" * 50):
        t = time.perf_counter()
        assert _matches(have, evil) is (have.endswith("@"))
        assert time.perf_counter() - t < 0.1, have[:5]
    t = time.perf_counter()
    for _ in range(1000):                                         # an alert list's worth, the worst pattern each time
        _matches("a" * 2000, "*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b")
    assert time.perf_counter() - t < 0.5
    cases = [("cairn-api", "cairn-api", True), ("cairn-api", "cairn-store", False), ("cairn-api", "cairn-*", True),
             ("cairn-api", "*-api", True), ("cairn-api", "c*-a*i", True), ("cairn-api", "*store*", False),
             ("cairn-api", "cairn-store|cairn-api", True), ("cairn-api", "cairn-.*", True), ("anything", ".*", True),
             ("", "*", True), ("cairn.api", "cairn.api", True), ("cairnXapi", "cairn.api", False),  # "." is literal
             ("ab", "a*b*", True), ("a", "a*a", False), ("c++", "c++", True)]                 # "+" is literal at run
    for have, want, ok in cases:
        assert _matches(have, want) is ok, (have, want)
    for bad in ("a(b|c)", "x+", "[ab]", "^a", "a$", "a?", "a{2}", "a\\d"):
        with pytest.raises(OpsError) as e:
            label_pattern(bad)
        assert e.value.status == 422 and "* for any run" in str(e.value)
    assert label_pattern("$service") == "$service" and label_pattern("${env}-*|x") == "${env}-*|x"
    with pytest.raises(OpsError) as e:
        validate_tile({"alerts": {"labels": {"service": "(a+)+$"}}})
    assert e.value.status == 422 and "alerts.labels.service" in str(e.value)


def test_a_pathological_label_pattern_renders_fast_and_is_refused_on_save(boards, backend):
    alerts = [{"id": str(i), "name": f"a{i}", "severity": "info", "state": "firing", "summary": "",
               "labels": {"service": "a" * 5000 if i % 2 else "cairn-api"}, "value": 1, "firing_since": 0,
               "started_at": 0, "silenced_by": None, "acknowledged_by": None, "incident_id": None} for i in range(1000)]
    boards.ops.obs.alerts = lambda state, limit: alerts
    board = boards.save_dashboard(ADMIN, {"name": "on call", "tiles": [
        {"title": "Firing", "alerts": {"labels": {"service": "$service"}}}],
        "controls": [{"id": "service", "kind": "variable", "default": "cairn-api"}]})
    t = time.perf_counter()
    out = boards.render(ADMIN, board["id"], {"controls": {"service": ".*" * 16 + "@"}})    # a value of the variable
    assert time.perf_counter() - t < 1.0
    assert out["tiles"][0]["ops"]["alerts"] == []
    out = boards.render(ADMIN, board["id"], {"controls": {"service": "cairn-*"}})
    assert out["tiles"][0]["ops"]["total"] == 500
    with pytest.raises(WorkspaceError) as e:
        boards.save_dashboard(ADMIN, {"name": "x", "tiles": [{"alerts": {"labels": {"service": "(.*)*@"}}}]})
    assert e.value.status == 422 and "alerts.labels.service" in str(e.value)
    r = _client(lambda who: build_ops_router(boards.ops, who)).post(
        "/v1/observability/tiles/preview", json={"tile": {"alerts": {"labels": {"service": "a+"}}}})
    assert r.status_code == 422 and "regular expression" in r.json()["detail"]


# ---- 2. the minimum refresh -----------------------------------------------------------------------------------------
def test_the_minimum_refresh_is_enforced_on_save_preview_and_import(boards, monkeypatch):
    assert validate_control({"kind": "time", "refresh": 30}, "time", 10)["refresh"] == 30
    assert validate_control({"kind": "time", "refresh": 0}, "time", 10)["refresh"] == 0       # off stays off
    with pytest.raises(OpsError) as e:
        validate_control({"kind": "time", "refresh": 1}, "time", 10)
    assert e.value.status == 422 and "at least 10s" in str(e.value)
    assert validate_control({"kind": "time", "refresh": 1}, "time", 10, clamp=True)["refresh"] == 10
    tile = {"title": "up", "promql": {"queries": [{"expr": "up"}]}}
    with pytest.raises(WorkspaceError) as e:
        boards.save_dashboard(ADMIN, {"name": "fast", "tiles": [tile], "controls": [{"id": "time", "kind": "time",
                                                                                      "refresh": 2}]})
    assert e.value.status == 422 and "CAIRN_OPS_MIN_REFRESH" in str(e.value)
    ok = boards.save_dashboard(ADMIN, {"name": "ok", "tiles": [tile], "controls": [{"id": "time", "kind": "time",
                                                                                    "refresh": 10}]})
    assert ok["controls"][0]["refresh"] == 10
    assert boards.render(ADMIN, ok["id"])["min_refresh_s"] == 10                   # the page polls no faster
    r = _client(lambda who: build_ops_router(boards.ops, who)).post("/v1/observability/tiles/preview", json={
        "tile": tile, "controls": [{"id": "time", "kind": "time", "refresh": 1}]})
    assert r.status_code == 422 and "at least 10s" in r.json()["detail"]
    # Grafana's "1s": raised to the minimum, and said so.
    doc = {"title": "g", "refresh": "1s", "panels": [{"type": "timeseries", "title": "up",
                                                      "datasource": {"type": "prometheus"}, "targets": [{"expr": "up"}]}]}
    out = boards.import_grafana(ADMIN, doc)
    assert out["dashboard"]["controls"][0]["refresh"] == 10
    assert [s for s in out["skipped"] if s.get("refresh") == "1s" and "at least 10s" in s["reason"]]
    # The operator's minimum, from CAIRN_OPS_MIN_REFRESH.
    boards.ops = _ops("http://127.0.0.1:1", env={"CAIRN_OPS_MIN_REFRESH": "1m"})
    assert boards.import_grafana(ADMIN, {**doc, "refresh": "30s"})["dashboard"]["controls"][0]["refresh"] == 60
    assert boards.ops.limits.public()["min_refresh_s"] == 60
    # The built-in dashboards (30s, 1m) still seed under a higher minimum: raised to it.
    from cairn.observability import builtin

    boards.ops = _ops("http://127.0.0.1:1", env={"CAIRN_OPS_MIN_REFRESH": "2m"})
    assert builtin.seed(boards)
    assert boards.dashboard(ADMIN, "ops-platform-overview")["controls"][0]["refresh"] == 120


# ---- 3. timeouts, range selectors and answer sizes ------------------------------------------------------------------
def test_backend_calls_carry_timeouts_and_bounded_ranges(backend):
    url = f"http://127.0.0.1:{backend.server_address[1]}"
    ops = _ops(url, env={"CAIRN_OPS_QUERY_TIMEOUT": "15s"})
    now = time.time()
    run = lambda tile: ops.run(ADMIN, validate_tile(tile), {}, now - 3600, now)  # noqa: E731
    run({"promql": {"queries": [{"expr": "sum(rate(x[5m]))"}]}})
    run({"promql": {"queries": [{"expr": "up"}], "instant": True}})
    run({"logql": {"queries": [{"expr": 'sum(count_over_time({a="b"}[1m]))'}], "mode": "metric"}, "view": "bars"})
    run({"logql": {"queries": [{"expr": '{a="b"}'}], "mode": "logs"}})
    run({"traces": {"query": "{ status = error }"}})
    by = {}
    for path, q in backend.seen:
        by.setdefault(path, []).append(q)
    assert [q["timeout"] for q in by["/api/v1/query_range"]] == ["14s"]          # Prometheus: below the client's 15s
    assert [q["timeout"] for q in by["/api/v1/query"]] == ["14s"]
    assert all("timeout" not in q for q in by["/loki/api/v1/query_range"] + by["/api/search"])   # no such parameter
    assert by["/api/search"][0]["spss"] == "3"
    # A range selector past CAIRN_OPS_MAX_RANGE (7d) is refused before any backend is asked.
    backend.seen.clear()
    for expr in ("rate(x[30d])", "max_over_time(x[8d:1h])", "sum(rate(x[1w1d]))"):
        with pytest.raises(OpsError) as e:
            run({"promql": {"queries": [{"expr": expr}]}})
        assert e.value.status == 422 and "at most 7d" in str(e.value), expr
    with pytest.raises(OpsError):
        run({"logql": {"queries": [{"expr": 'sum(count_over_time({a="b"}[30d]))'}], "mode": "metric"}, "view": "bars"})
    with pytest.raises(OpsError) as e:                                      # a $variable is filled in first
        ops.run(ADMIN, validate_tile({"promql": {"queries": [{"expr": "increase(x[$window])"}]}}), {"window": "30d"},
                now - 3600, now)
    assert e.value.status == 422 and "[30d]" in str(e.value)
    assert backend.seen == []
    run({"promql": {"queries": [{"expr": 'rate(x{a=~"[a-z]+"}[7d])'}]}})    # at the limit, and a label regex: fine


def test_a_huge_answer_is_cut_off_while_it_is_read(backend):
    url = f"http://127.0.0.1:{backend.server_address[1]}"
    ops = _ops(url, env={"CAIRN_OPS_MAX_RESPONSE_BYTES": str(64 * 1024)})
    now = time.time()
    backend.big = 500                                                       # about 500 KB
    for chunked in (False, True):
        backend.chunked = chunked
        with pytest.raises(OpsError) as e:
            ops.run(ADMIN, validate_tile({"promql": {"queries": [{"expr": "up"}]}}), {}, now - 600, now)
        assert e.value.status == 422 and "at most 65536" in str(e.value), chunked


# ---- 6. malformed Grafana JSON --------------------------------------------------------------------------------------
P = {"type": "timeseries", "title": "x", "datasource": {"type": "prometheus"}, "targets": [{"expr": "up"}]}


@pytest.mark.parametrize("doc,field", [
    ({"panels": [{**P, "span": "6"}]}, "span"),
    ({"panels": [{**P, "gridPos": {"w": "12"}}]}, "gridPos.w"),
    ({"panels": [{**P, "gridPos": [1]}]}, "gridPos"),
    ({"rows": ["x"], "panels": [P]}, "rows[0]"),
    ({"panels": [{"type": "row", "panels": 5}, P]}, "panels[0].panels"),
    ({"templating": [{"name": "a"}], "panels": [P]}, "templating"),
    ({"templating": {"list": {"a": 1}}, "panels": [P]}, "templating.list"),
    ({"panels": [{**P, "fieldConfig": [1]}]}, "fieldConfig"),
    ({"panels": [{**P, "fieldConfig": {"defaults": [1]}}]}, "fieldConfig.defaults"),
    ({"panels": [{**P, "fieldConfig": {"defaults": {"thresholds": [1]}}}]}, "thresholds"),
    ({"panels": [{**P, "fieldConfig": {"defaults": {"custom": {"fillOpacity": "5"}}}}]}, "fillOpacity"),
    ({"panels": [{**P, "fill": "5"}]}, "fill"),
    ({"time": "now-1h", "panels": [P]}, "time"),
    ({"panels": {"a": P}}, "panels"),
    ({"panels": [{**P, "targets": {"expr": "up"}}]}, "targets"),
    ({"templating": {"list": [{"name": "a", "type": "custom", "current": "x", "query": "a,b"}]}, "panels": [P]},
     "current"),
])
def test_malformed_grafana_json_is_a_422_naming_the_field(doc, field, boards):
    with pytest.raises(OpsError) as e:
        from_grafana(doc)
    assert e.value.status == 422 and field in str(e.value), str(e.value)
    with pytest.raises(WorkspaceError) as e:
        boards.import_grafana(ADMIN, doc)
    assert e.value.status == 422 and field in str(e.value)


def test_the_grafana_import_route_answers_422_and_limits_the_body(boards):
    c = _client(lambda who: build_boards_router(boards, who))
    r = c.post("/sql/dashboards/import/grafana", json={"panels": [{**P, "gridPos": {"w": "wide"}}]})
    assert r.status_code == 422 and "gridPos.w" in r.json()["detail"]
    r = c.post("/sql/dashboards/import/grafana", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    big = json.dumps({"panels": [P], "description": "x" * (2 << 20)}).encode()
    r = c.post("/sql/dashboards/import/grafana", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and "2 MB" in r.json()["detail"]

    def chunks():                                                       # no Content-Length: counted as it arrives
        for i in range(0, len(big), 65536):
            yield big[i:i + 65536]

    r = c.post("/sql/dashboards/import/grafana", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = c.post("/sql/dashboards/import/grafana", json={"title": "fine", "panels": [P]})
    assert r.status_code == 201 and r.json()["dashboard"]["tiles"][0]["promql"]["queries"][0]["expr"] == "up"


# ---- 7. one tile's unexpected error ---------------------------------------------------------------------------------
def test_one_tiles_unexpected_error_shows_in_that_tile_only(boards, caplog):
    from pydantic import BaseModel

    class Strict(BaseModel):
        n: int

    def broken(state, limit):
        raise KeyError("severity")

    boards.ops.obs.alerts = broken
    boards.ops.obs.slo_status = lambda sid: Strict.model_validate({"n": "not a number"})   # a ValidationError
    board = boards.save_dashboard(ADMIN, {"name": "mixed", "tiles": [
        {"title": "Firing", "alerts": {"state": "firing"}},
        {"title": "SLO", "slo": {"id": "api-answers"}},
        {"title": "up", "promql": {"queries": [{"expr": "up"}]}}]})
    with caplog.at_level("ERROR"):
        out = boards.render(ADMIN, board["id"])
    t = {x["title"]: x for x in out["tiles"]}
    assert t["Firing"]["status"] == 500 and "KeyError" in t["Firing"]["error"] and "ops" not in t["Firing"]
    assert t["SLO"]["status"] == 500 and "ValidationError" in t["SLO"]["error"]
    assert t["up"]["ops"]["series"] and "error" not in t["up"]
    assert any("KeyError" in r.getMessage() for r in caplog.records)
    # The preview of such a tile answers 500 with the same message, not an unhandled error.
    r = _client(lambda who: build_ops_router(boards.ops, who)).post(
        "/v1/observability/tiles/preview", json={"tile": {"alerts": {"state": "firing"}}})
    assert r.status_code == 500 and "KeyError" in r.json()["detail"]
