"""Observability without its backends: what telemetry may carry (redaction, on the SDK's real pipeline), the API's
definitions (SLOs, alert rules, silences) and their checks, incidents worked by hand and by an agent, and that a
rule whose backend cannot be read keeps its alerts as they were. The backends themselves: test_observability_e2e."""

import socket
import time

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.observability import redact
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
H = {"X-Cairn-Actor": ADMIN}


# ---- redaction -------------------------------------------------------------------------------------------------
def test_sql_keeps_its_shape_without_its_values():
    sql = "SELECT name, salary FROM hr.pay WHERE email = 'jane@acme.com' AND id IN (1, 2.5) -- who\n AND x = $$a$$"
    out = redact.scrub_sql(sql)
    assert out == "SELECT name, salary FROM hr.pay WHERE email = ? AND id IN (?, ?) AND x = ?"
    assert redact.scrub_sql("SELECT t1.c2 FROM s3.t1") == "SELECT t1.c2 FROM s3.t1"      # identifiers stay


def test_credentials_in_free_text_urls_and_attributes():
    text = ("Authorization: Bearer abc.def-ghi_jkl token=s3cr3t&x=1 jwt eyJhbGciOi.eyJzdWIiOiJ4.sig "
            "postgres://cairn:hunter2@db:5432/cairn AKIAABCDEFGHIJKLMNOP ca_0123456789abcdef0123")
    out = redact.scrub_text(text)
    for secret in ("abc.def-ghi_jkl", "s3cr3t", "eyJhbGciOi", "hunter2", "AKIAABCDEFGHIJKLMNOP", "ca_0123456789abcdef"):
        assert secret not in out
    assert redact.scrub_url("https://u:p@hooks.slack.com/services/T0AB/B0CD/xyzSECRET?token=1&a=2") == \
        "https://hooks.slack.com/services/{}/{}/{}?[redacted]=[redacted]&a=[redacted]"
    attrs = redact.scrub_attributes({
        "http.route": "/v1/trino/{given}/allow", "url.path": "/v1/trino/the-key-itself/allow",
        "url.full": "http://cairn:8800/v1/trino/the-key-itself/allow?x=1", "http.request.header.authorization": ("x",),
        "http.request.header.content_type": ("application/json",), "db.statement": "UPDATE t SET a = 'masked' WHERE b = 7",
        "cairn.api_key": "abc", "http.status_code": 200}, server=True)
    assert attrs["url.path"] == "/v1/trino/{given}/allow" and "the-key-itself" not in str(attrs)
    assert "http.request.header.authorization" not in attrs and "http.request.header.content_type" in attrs
    assert attrs["db.statement"] == "UPDATE t SET a = ? WHERE b = ?" and attrs["cairn.api_key"] == "[redacted]"
    assert attrs["http.status_code"] == 200
    err = "(sqlite3.IntegrityError) UNIQUE failed [SQL: INSERT INTO t (a) VALUES (?)] [parameters: ('jane@acme.com',)]"
    assert "jane@acme.com" not in redact.scrub_error(err) and "INSERT INTO t" in redact.scrub_error(err)
    with pytest.raises(redact.Unredactable):
        redact.check_clean(["fine", ("Bearer abcdefghijkl",)])


def test_spans_and_logs_are_cleaned_on_the_sdks_pipeline_and_dropped_when_they_cannot_be():
    pytest.importorskip("opentelemetry.sdk")
    import logging

    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind, StatusCode

    from cairn.observability.telemetry import RedactingLogProcessor, RedactingSpanProcessor

    spans = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(RedactingSpanProcessor(SimpleSpanProcessor(spans)))
    tracer = tp.get_tracer("t")
    with tracer.start_as_current_span("POST /v1/trino/{given}/allow", kind=SpanKind.SERVER, attributes={
            "http.route": "/v1/trino/{given}/allow", "url.path": "/v1/trino/k-123/allow",
            "db.statement": "SELECT * FROM t WHERE ssn = '123-45-6789'"}) as s:
        s.record_exception(ValueError("refused 'jane@acme.com' with Bearer abcdefghijklmnop"))
        s.set_status(StatusCode.ERROR, "token=hunter2")
    (got,) = spans.get_finished_spans()
    blob = str(got.to_json())
    for secret in ("k-123", "123-45-6789", "jane@acme.com", "abcdefghijklmnop", "hunter2"):
        assert secret not in blob
    assert got.attributes["url.path"] == "/v1/trino/{given}/allow"
    # A credential in a name is cleaned; a span that cannot be cleaned (a URL that cannot be parsed) is not exported.
    with tracer.start_as_current_span("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"):
        pass
    assert spans.get_finished_spans()[-1].name == "[redacted]"
    with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT, attributes={"url.full": "http://[::1/x?token=t0k"}):
        pass
    assert len(spans.get_finished_spans()) == 2

    logs = InMemoryLogRecordExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(RedactingLogProcessor(SimpleLogRecordProcessor(logs)))
    log = logging.getLogger("test.redact")
    handler = LoggingHandler(logger_provider=lp)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("calling %s with password=%s", "https://u:pw@host/x?key=1", "p4ss")
        try:
            raise RuntimeError("[SQL: SELECT 1 WHERE a = 'secret-value'] [parameters: ('x',)]")
        except RuntimeError:
            log.exception("failed")
    finally:
        log.removeHandler(handler)
    records = [r.log_record if hasattr(r, "log_record") else r for r in logs.get_finished_logs()]
    text = " ".join(str(r.body) + str(dict(r.attributes or {})) for r in records)
    assert len(records) == 2 and "p4ss" not in text and ":pw@" not in text and "secret-value" not in text


# ---- the API -----------------------------------------------------------------------------------------------------
@pytest.fixture
def client(settings, graph, monkeypatch):
    for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL", "CAIRN_OBS_TRACE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    return TestClient(create_api_app(s)), s


SLO = {"id": "api-availability", "name": "API availability", "service": "cairn-api", "objective": 99.9,
       "indicator": {"type": "ratio", "errors": 'sum(rate(x_total{code="5xx"}[$window]))',
                     "total": "sum(rate(x_total[$window]))"}}


def test_definitions_are_checked_and_kept(client):
    c, s = client
    assert c.post("/v1/observability/slos", json=SLO, headers=H).status_code == 201
    got = c.get("/v1/observability/slos/api-availability", headers=H).json()
    assert got["indicator"] == SLO["indicator"] and len(got["burn_alerts"]) == 4 and got["window"] == "30d"
    assert c.post("/v1/observability/slos", json=SLO, headers=H).status_code == 409
    bad = [{**SLO, "id": "b1", "indicator": {"type": "ratio", "errors": "sum(x)", "total": "sum(y)"}},      # no $window
           {**SLO, "id": "b2", "objective": 100}, {**SLO, "id": "b3", "window": "a month"},
           {**SLO, "id": "b4", "indicator": {"type": "latency", "metric": "h", "threshold": 5, "selector": "x"}},
           {**SLO, "id": "b5", "routes": ["no-such-route"]}]
    for body in bad:
        r = c.post("/v1/observability/slos", json=body, headers=H)
        assert r.status_code == 422, (body["id"], r.text)
    lat = {**SLO, "id": "api-latency", "indicator": {"type": "latency", "metric": "traces_span_metrics_duration_milliseconds",
                                                      "selector": 'service_name="cairn-api"', "threshold": 500}}
    assert c.post("/v1/observability/slos", json=lat, headers=H).status_code == 201
    from cairn.observability.service import Indicator

    q = Indicator.model_validate(lat["indicator"]).bad_ratio("5m")
    assert 'traces_span_metrics_duration_milliseconds_bucket{service_name="cairn-api",le="500"}[5m]' in q
    rule = {"id": "store-errors", "name": "Store errors", "expr": "sum(rate(x[5m])) > 1", "pending_for": "5m",
            "severity": "critical"}
    assert c.post("/v1/observability/alert-rules", json=rule, headers=H).status_code == 201
    assert c.put("/v1/observability/alert-rules/store-errors", json={**rule, "pending_for": "1 minute"},
                 headers=H).status_code == 422
    r = c.put("/v1/observability/alert-rules/store-errors", json={**rule, "pending_for": "10m"}, headers=H)
    assert r.status_code == 200 and r.json()["pending_for"] == "10m"
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    r = c.post("/v1/observability/silences", json={"id": "maint", "matchers": [{"name": "service", "value": "x"}],
                                                   "ends_at": end, "comment": "maintenance"}, headers=H)
    assert r.status_code == 201 and r.json()["active"] is True
    far = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 100 * 86400))
    assert c.post("/v1/observability/silences", json={"id": "long", "matchers": [{"name": "a", "value": "b"}],
                                                      "ends_at": far, "comment": "x"}, headers=H).status_code == 422
    assert c.post("/v1/observability/silences", json={"id": "badre", "matchers": [{"name": "a", "value": "(",
                                                                                    "regex": True}],
                                                      "ends_at": end, "comment": "x"}, headers=H).status_code == 422
    assert c.delete("/v1/observability/silences/maint", headers=H).status_code == 204
    assert c.get("/v1/observability/silences/maint", headers=H).status_code == 404
    actions = [e["action"] for e in s.audit.entries(category="observability")]
    assert "observability.slo_created" in actions and "observability.silence_deleted" in actions


def test_only_operators(client):
    c, _ = client
    other = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    for method, path in (("GET", "/v1/observability/overview"), ("GET", "/v1/observability/incidents"),
                         ("POST", "/v1/observability/slos"), ("GET", "/v1/observability/logs?service=x")):
        assert c.request(method, path, headers=other, json=SLO if method == "POST" else None).status_code == 403


def test_without_backends_it_says_what_to_configure(client):
    c, _ = client
    for path in ("/v1/observability/health", "/v1/observability/traces", "/v1/observability/logs?service=cairn-api",
                 f"/v1/observability/journey/{'a' * 32}"):
        r = c.get(path, headers=H)
        assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert c.get("/v1/observability/journey/not-a-trace", headers=H).status_code == 422
    assert c.get("/v1/observability/logs", headers=H).status_code == 422          # neither a trace nor a component
    ov = c.get("/v1/observability/overview", headers=H).json()
    assert ov["backends"]["prometheus"] is False and ov["open_incidents"] == 0


def test_journey_links_out_to_a_vendor_backend(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_OBS_TRACE_URL", "https://ui.example-vendor.test/trace/{trace_id}")
    monkeypatch.setenv("CAIRN_OBS_VENDOR", "Example")
    for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    c = TestClient(create_api_app(s))
    j = c.get(f"/v1/observability/journey/{'b' * 32}", headers=H).json()
    assert j["source"] == "link" and j["links"]["trace"] == f"https://ui.example-vendor.test/trace/{'b' * 32}"


def test_incidents_by_hand_and_by_an_agent(client):
    c, s = client
    r = c.post("/v1/observability/incidents", json={"title": "Queries are slow", "severity": "warning",
                                                    "services": ["trino"]}, headers=H)
    assert r.status_code == 201
    iid = r.json()["id"]
    s.observability.record_deploy(ADMIN, {"component": "trino", "version": "484"})
    s.audit.record("governance", "policy.updated", ADMIN, "analysts", {})
    r = c.post(f"/v1/observability/incidents/{iid}/notes", headers=H,
               json={"kind": "hypothesis", "text": "the new policy adds a row filter; token=abc123 (pasted by mistake)"})
    assert r.status_code == 200
    tl = r.json()["timeline"]
    assert {"status", "deploy", "change", "hypothesis"} <= {e["kind"] for e in tl}
    assert "abc123" not in str(tl)                                         # notes are cleaned too
    assert c.post(f"/v1/observability/incidents/{iid}/notes", json={"kind": "rant", "text": "x"}, headers=H).status_code == 422
    assert c.post(f"/v1/observability/incidents/{iid}/assign", json={"assignee": "nobody"}, headers=H).status_code == 422
    assert c.post(f"/v1/observability/incidents/{iid}/assign", json={"assignee": ADMIN}, headers=H).json()["assignee"] == ADMIN
    assert c.post(f"/v1/observability/incidents/{iid}/acknowledge", headers=H).json()["status"] == "acknowledged"
    assert c.post(f"/v1/observability/incidents/{iid}/acknowledge", headers=H).status_code == 409
    done = c.post(f"/v1/observability/incidents/{iid}/resolve", json={"summary": "reverted the policy"}, headers=H).json()
    assert done["status"] == "resolved" and done["summary"] == "reverted the policy"
    assert c.get("/v1/observability/incidents", params={"status": "active"}, headers=H).json()["items"] == []
    ctx = c.get(f"/v1/observability/incidents/{iid}/context", headers=H).json()
    assert ctx["incident"]["id"] == iid and "error" in ctx["health"] and "error" in ctx["error_traces"]
    assert c.post(f"/v1/observability/incidents/{iid}/reopen", headers=H).json()["status"] == "open"


def test_a_rule_whose_backend_cannot_be_read_keeps_its_alerts(settings, graph, monkeypatch):
    """Fail closed: a firing alert is never resolved because Prometheus went away; the rule says why."""
    with socket.socket() as sock:                     # a port nothing listens on: Prometheus is down
        sock.bind(("127.0.0.1", 0))
        dead = sock.getsockname()[1]
    monkeypatch.setenv("CAIRN_OBS_PROMETHEUS_URL", f"http://127.0.0.1:{dead}")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    svc = s.observability
    svc.create("alert_rule", ADMIN, {"id": "r1", "name": "R1", "expr": "up == 0", "severity": "critical"})
    now = int(time.time() * 1000)
    labels = {"job": "cairn-api"}
    from cairn.observability.service import _fingerprint

    svc._advance("rule:r1", {"name": "R1", "severity": "critical"}, {_fingerprint("rule:r1", labels): {
        "labels": labels, "value": 0.0}}, now)                   # it fired while Prometheus answered
    (a,) = svc.alerts(state="firing")
    assert a["incident_id"]
    out = svc.evaluate(now + 60_000)
    assert out["errors"] == 1
    assert [x["id"] for x in svc.alerts(state="firing")] == [a["id"]]
    assert "prometheus did not answer" in svc.get("alert_rule", "r1")["last_evaluation"]["error"]
    # Deleting the rule ends its alerts (and says so on the incident).
    svc.remove("alert_rule", ADMIN, "r1")
    assert svc.alerts(state="firing") == [] and svc.alerts(state="resolved")[0]["id"] == a["id"]
    assert any(e["kind"] == "alert_resolved" for e in svc.incident(a["incident_id"])["timeline"])
