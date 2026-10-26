import pytest
from sqlalchemy import create_engine, text

from aegis.common.proposals import assertion_urn
from aegis.quality.collectors import ConnectionRegistry, DataHubSignalCollector, SqlCollector
from aegis.quality.engine import QualityEngine
from aegis.quality.models import MonitorSpec, validate_readonly_sql
from aegis.quality.scheduler import Scheduler, next_run_ms
from aegis.quality.store import QualityStore

from conftest import DS

HOUR = 3_600_000
T0 = 1_760_000_000_000


@pytest.fixture
def warehouse(tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, email TEXT, method TEXT, ts TEXT)"))
    return url, e


def _fill(e, n, method="card"):
    with e.begin() as c:
        c.execute(text("DELETE FROM orders"))
        for i in range(n):
            c.execute(text("INSERT INTO orders VALUES (:i, :e, :m, '2026-01-01T00:00:00')"),
                      {"i": i, "e": None if i % 10 == 0 else f"u{i}@x.io", "m": method})


def _engine(engine, graph, warehouse_url, notifier=None):
    reg = ConnectionRegistry({"wh": {"url": warehouse_url}})
    return QualityEngine(QualityStore(engine), graph, {"sql": SqlCollector(reg), "datahub": DataHubSignalCollector(graph)},
                         notifier)


def test_spec_validation():
    with pytest.raises(ValueError):
        MonitorSpec(name="x", dataset_urn=DS, type="custom_sql", config={"connection": "wh", "sql": "DELETE FROM t"})
    with pytest.raises(ValueError):
        MonitorSpec(name="x", dataset_urn=DS, type="volume", config={"source": "sql"})
    with pytest.raises(ValueError):
        MonitorSpec(name="x", dataset_urn="not-an-urn", type="volume")
    for bad in ("SELECT 1; DROP TABLE x", "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", "UPDATE t SET a=1"):
        with pytest.raises(ValueError):
            validate_readonly_sql(bad)
    validate_readonly_sql("SELECT count(*) FROM orders WHERE created_at > now() - interval '1 day'")


def test_volume_anomaly_raises_and_resolves_incident(engine, graph, warehouse):
    url, wh = warehouse
    events = []
    qe = _engine(engine, graph, url, lambda m, ev, kind: events.append(kind))
    m = qe.store.create_monitor(MonitorSpec(
        name="orders volume", dataset_urn=DS, type="volume", severity="HIGH",
        config={"source": "sql", "connection": "wh", "table": "orders", "seasonality": None, "min_history": 10}), "urn:li:corpuser:alice")
    qe.sync_monitor(m)
    info = graph.get_aspect(assertion_urn(m.id), "assertionInfo")
    assert info["type"] == "CUSTOM" and info["source"]["type"] == "NATIVE"

    for i in range(12):
        _fill(wh, 100 + (i % 3))
        ev = qe.evaluate(qe.store.get_monitor(m.id), now=T0 + i * HOUR)
    assert ev.status == "PASS"
    assert qe.store.get_monitor(m.id).status == "ACTIVE"

    _fill(wh, 5)
    bad = qe.evaluate(qe.store.get_monitor(m.id), now=T0 + 13 * HOUR)
    assert bad.status == "FAIL" and bad.emitted
    run_event = graph.get_timeseries(assertion_urn(m.id), "assertionRunEvent")[0]
    assert run_event["result"]["type"] == "FAILURE" and run_event["result"]["actualAggValue"] == 5.0
    incident = qe.store.get_state(m.id)["open_incident_urn"]
    assert incident and graph.get_aspect(incident, "incidentInfo")["status"]["state"] == "ACTIVE"
    assert graph.get_aspect(incident, "incidentInfo")["type"] == "VOLUME"

    # Second consecutive failure must not open a second incident.
    qe.evaluate(qe.store.get_monitor(m.id), now=T0 + 14 * HOUR)
    assert qe.store.get_state(m.id)["open_incident_urn"] == incident

    _fill(wh, 101)
    ok = qe.evaluate(qe.store.get_monitor(m.id), now=T0 + 15 * HOUR)
    assert ok.status == "PASS"
    assert graph.get_aspect(incident, "incidentInfo")["status"]["state"] == "RESOLVED"
    assert qe.store.get_state(m.id)["open_incident_urn"] is None
    assert events == ["failing", "recovered"]


def test_human_resolved_incident_is_not_touched(engine, graph, warehouse):
    url, wh = warehouse
    qe = _engine(engine, graph, url)
    m = qe.store.create_monitor(MonitorSpec(name="v", dataset_urn=DS, type="volume",
                                            config={"source": "sql", "connection": "wh", "table": "orders", "min_rows": 50}), None)
    _fill(wh, 1)
    qe.evaluate(m, now=T0)
    urn = qe.store.get_state(m.id)["open_incident_urn"]
    info = graph.get_aspect(urn, "incidentInfo")
    info["status"]["state"] = "RESOLVED"
    info["status"]["message"] = "closed by human"
    graph.aspects[(urn, "incidentInfo")] = info
    before = len(graph.emitted)
    qe.evaluate(m, now=T0 + HOUR)  # still failing → no new incident, no reopen
    assert all(e["aspectName"] != "incidentInfo" for e in graph.emitted[before:])
    _fill(wh, 100)
    qe.evaluate(m, now=T0 + 2 * HOUR)
    assert graph.get_aspect(urn, "incidentInfo")["status"]["message"] == "closed by human"


def test_idempotent_run_ids(engine, graph, warehouse):
    url, wh = warehouse
    _fill(wh, 10)
    qe = _engine(engine, graph, url)
    m = qe.store.create_monitor(MonitorSpec(name="v", dataset_urn=DS, type="volume",
                                            config={"source": "sql", "connection": "wh", "table": "orders"}), None)
    a = qe.evaluate(m, now=T0, scheduled_ms=T0)
    b = qe.evaluate(m, now=T0 + 5, scheduled_ms=T0)
    assert a.id == b.id
    assert len(qe.store.evaluations(m.id)) == 1


def test_field_metric_and_distribution_sql(engine, graph, warehouse):
    url, wh = warehouse
    _fill(wh, 100)
    qe = _engine(engine, graph, url)
    nulls = qe.store.create_monitor(MonitorSpec(name="email nulls", dataset_urn=DS, type="field_metric", config={
        "source": "sql", "connection": "wh", "table": "orders", "field": "email", "metric": "null_pct", "max": 5}), None)
    ev = qe.evaluate(nulls, now=T0)
    assert ev.metric_value == pytest.approx(10.0) and ev.status == "FAIL"

    dist = qe.store.create_monitor(MonitorSpec(name="method drift", dataset_urn=DS, type="distribution", config={
        "source": "sql", "connection": "wh", "table": "orders", "field": "method"}), None)
    assert qe.evaluate(dist, now=T0).status == "TRAINING"
    assert qe.evaluate(dist, now=T0 + HOUR).status == "PASS"
    _fill(wh, 100, method="crypto")
    drift = qe.evaluate(dist, now=T0 + 2 * HOUR)
    assert drift.status == "FAIL" and drift.details["psi"] > 1


def test_freshness_from_datahub_operations(engine, graph):
    qe = QualityEngine(QualityStore(engine), graph, {"datahub": DataHubSignalCollector(graph)})
    for i in range(8):
        graph.emit([{"entityUrn": DS, "aspectName": "operation", "entityType": "dataset", "changeType": "UPSERT",
                     "aspect": {"json": {"timestampMillis": T0 + i * HOUR, "lastUpdatedTimestamp": T0 + i * HOUR,
                                         "operationType": "INSERT"}}}])
    m = qe.store.create_monitor(MonitorSpec(name="fresh", dataset_urn=DS, type="freshness"), None)
    assert qe.evaluate(m, now=T0 + 7 * HOUR + 10 * 60_000).status == "PASS"
    stale = qe.evaluate(m, now=T0 + 7 * HOUR + 5 * HOUR)
    assert stale.status == "FAIL"
    info = graph.get_aspect(assertion_urn(m.id), "assertionInfo")
    if info:  # only emitted if synced; sync and check native FRESHNESS shape
        assert info["type"] == "FRESHNESS"
    qe.sync_monitor(m)
    assert graph.get_aspect(assertion_urn(m.id), "assertionInfo")["freshnessAssertion"]["schedule"]["type"] == "SINCE_THE_LAST_CHECK"


def test_schema_monitor_from_datahub(engine, graph):
    qe = QualityEngine(QualityStore(engine), graph, {"datahub": DataHubSignalCollector(graph)})
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id", "nativeDataType": "INT"}]}
    m = qe.store.create_monitor(MonitorSpec(name="schema", dataset_urn=DS, type="schema"), None)
    assert qe.evaluate(m, now=T0).status == "TRAINING"
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id", "nativeDataType": "BIGINT"}]}
    assert qe.evaluate(m, now=T0 + HOUR).status == "FAIL"


def test_collector_error_is_recorded_not_raised(engine, graph):
    qe = QualityEngine(QualityStore(engine), graph, {"datahub": DataHubSignalCollector(graph)})
    m = qe.store.create_monitor(MonitorSpec(name="vol", dataset_urn=DS, type="volume"), None)
    ev = qe.evaluate(m, now=T0)
    assert ev.status == "ERROR" and "datasetProfile" in ev.message
    assert graph.get_timeseries(assertion_urn(m.id), "assertionRunEvent")[0]["result"]["type"] == "ERROR"


def test_gms_outage_buffers_and_flushes(engine, warehouse):
    url, wh = warehouse
    _fill(wh, 10)

    class Flaky:
        def __init__(self):
            self.down, self.emitted = True, []

        def emit(self, mcps):
            if self.down:
                raise ConnectionError("gms down")
            self.emitted.extend(mcps)

        def get_aspect(self, *_):
            return None

    g = Flaky()
    qe = _engine(engine, g, url)
    m = qe.store.create_monitor(MonitorSpec(name="v", dataset_urn=DS, type="volume",
                                            config={"source": "sql", "connection": "wh", "table": "orders"}), None)
    ev = qe.evaluate(m, now=T0)
    assert not ev.emitted and len(qe.store.unemitted()) == 1
    g.down = False
    assert qe.flush_unemitted() == 1
    assert qe.store.unemitted() == [] and g.emitted[0]["aspectName"] == "assertionRunEvent"


def test_feedback_controls_baseline(engine, graph, warehouse):
    url, wh = warehouse
    _fill(wh, 10)
    qe = _engine(engine, graph, url)
    m = qe.store.create_monitor(MonitorSpec(name="v", dataset_urn=DS, type="volume",
                                            config={"source": "sql", "connection": "wh", "table": "orders"}), None)
    ev = qe.evaluate(m, now=T0)
    assert len(qe.store.history(m.id)) == 1
    qe.store.set_feedback(ev.id, "REAL")
    assert qe.store.history(m.id) == []
    assert qe.store.get_evaluation(ev.id).feedback == "REAL"


def test_scheduler_runs_due_and_reschedules(engine, graph, warehouse):
    url, wh = warehouse
    _fill(wh, 10)
    qe = _engine(engine, graph, url)
    cron = qe.store.create_monitor(MonitorSpec(name="c", dataset_urn=DS, type="volume", schedule={"cron": "0 * * * *"},
                                               config={"source": "sql", "connection": "wh", "table": "orders"}), None)
    qe.store.create_monitor(MonitorSpec(name="i", dataset_urn=DS, type="volume", schedule={"interval_seconds": 600},
                                        config={"source": "sql", "connection": "wh", "table": "orders"}), None)
    s = Scheduler(qe, max_workers=2)
    assert s.run_once(now=T0) == 2
    assert s.run_once(now=T0 + 1000) == 0
    nxt = qe.store.get_monitor(cron.id).next_run_at_ms
    assert nxt == next_run_ms(cron, T0) and nxt % HOUR == 0
    assert s.run_once(now=T0 + 2 * HOUR) == 2


def test_assertion_run_summary_is_maintained_by_aegis(engine, graph):
    """OSS GMS cannot patch assertionRunSummary, so Aegis keeps it current for its assertions."""
    from aegis.quality.models import Evaluation

    store = QualityStore(engine)
    qe = QualityEngine(store, graph, {})
    m = store.create_monitor(MonitorSpec(name="rows", dataset_urn=DS, type="volume", config={"min_rows": 1}), None)
    qe.sync_monitor(m)
    urn = assertion_urn(m.id)
    assert graph.get_aspect(urn, "assertionRunSummary")["assertionStatus"] == "INIT"
    qe.sync_monitor(m)                                       # re-sync never resets it
    def ev(status, ts):
        return Evaluation(id=f"e{ts}", monitor_id=m.id, ts="", ts_ms=ts, run_id=f"r{ts}", status=status,
                          message="", metric_value=None, expected_low=None, expected_high=None, score=None)
    graph.emit(qe._summary_mcps(m, ev("FAIL", 1000)))
    graph.emit(qe._summary_mcps(m, ev("PASS", 2000)))
    s = graph.get_aspect(urn, "assertionRunSummary")
    assert s["lastFailedAtMillis"] == 1000 and s["lastPassedAtMillis"] == 2000 and s["assertionStatus"] == "PASSING"
