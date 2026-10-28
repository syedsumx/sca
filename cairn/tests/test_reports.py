"""Reports: coverage by domain and platform with drill-down, adoption, quality and governance
figures, daily snapshots and CSV export."""

import pytest

from cairn.collab.service import CollabService
from cairn.governance.personas import PersonaEngine
from cairn.quality.models import MonitorSpec
from cairn.quality.store import QualityStore
from cairn.reports.service import ReportService, incident_durations, to_csv

A = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.public.ledger,PROD)"
B = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.public.fx,PROD)"
C = "urn:li:dataset:(urn:li:dataPlatform:postgres,app.public.users,PROD)"
FIN = "urn:li:domain:finance"
NOW = 1_800_000_000_000


@pytest.fixture
def reports(engine, graph):
    for u in (A, B, C):
        graph.aspects[(u, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    graph.aspects[(A, "datasetProperties")] = {"description": "General ledger"}
    graph.aspects[(B, "editableDatasetProperties")] = {"description": "FX rates"}
    graph.aspects[(A, "domains")] = {"domains": [FIN]}
    graph.aspects[(B, "domains")] = {"domains": [FIN]}
    graph.aspects[(A, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:ana", "type": "DATAOWNER"}]}
    graph.aspects[(C, "glossaryTerms")] = {"terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Email"}]}
    store = QualityStore(engine)
    store.create_monitor(MonitorSpec(name="v", dataset_urn=A, type="volume"), None)
    collab = CollabService(engine)
    for who in ("ana", "bob"):
        collab.record_view(f"urn:li:corpuser:{who}", A, ts_ms=NOW - 3_600_000)
    collab.record_view("urn:li:corpuser:old", A, ts_ms=NOW - 20 * 86_400_000)
    return ReportService(engine, graph, store, PersonaEngine(engine)), graph


def test_coverage_by_domain_and_platform(reports):
    svc, _ = reports
    snap = svc.compute(NOW)
    assert snap["assets"] == 3
    assert snap["coverage"]["overall"] == {"description": 66.7, "owner": 33.3, "classification": 33.3,
                                           "certified": 0.0, "monitored": 33.3}
    fin = next(r for r in snap["coverage"]["by_domain"] if r["key"] == FIN)
    assert fin == {"key": FIN, "assets": 2, "description": 100.0, "owner": 50.0, "classification": 0.0,
                   "certified": 0.0, "monitored": 50.0}
    assert [r["key"] for r in snap["coverage"]["by_platform"]] == ["snowflake", "postgres"]


def test_drill_down_lists_the_assets_behind_a_figure(reports):
    svc, _ = reports
    assert [f["urn"] for f in svc.assets(missing="owner", domain=FIN)] == [B]
    assert [f["urn"] for f in svc.assets(has="classification")] == [C]
    assert [f["urn"] for f in svc.assets(missing="description", platform="postgres")] == [C]
    with pytest.raises(ValueError):
        svc.assets(missing="vibes")


def test_adoption_quality_and_governance(reports):
    svc, _ = reports
    snap = svc.compute(NOW)
    assert snap["adoption"]["weekly_active_users"] == 2 and snap["adoption"]["monthly_active_users"] == 3
    assert snap["adoption"]["asset_views_7d"] == 2
    assert snap["quality"]["open_incidents"] == 0 and snap["quality"]["monitored_pct"] == 33.3
    assert snap["governance"] == {"personas": 0, "purposes": 0, "active_grants": 0, "grants_expiring_7d": 0}


def test_incident_durations():
    h = 3_600_000
    evs = [(0, "PASS"), (h, "FAIL"), (2 * h, "FAIL"), (4 * h, "PASS"), (5 * h, "WARN"), (6 * h, "FAIL"), (7 * h, "PASS")]
    assert incident_durations(evs, since_ms=0) == [3.0, 1.0]
    assert incident_durations(evs, since_ms=5 * h) == [1.0]
    assert incident_durations([(0, "FAIL")], 0) == []                       # still open


def test_snapshots_are_daily_and_export_as_csv(reports):
    svc, graph = reports
    svc.compute(NOW - 86_400_000)
    svc.compute(NOW)
    svc.compute(NOW + 1000)                                               # same day: replaced
    assert [h["day"] for h in svc.history()] == ["2027-01-14", "2027-01-15"]
    assert not svc.due(NOW + 2000) and svc.due(NOW + 86_400_000)
    lines = to_csv(svc.latest()).strip().splitlines()
    assert lines[0] == "day,group,key,assets,description_pct,owner_pct,classification_pct,certified_pct,monitored_pct"
    assert f"2027-01-15,domain,{FIN},2,100.0,50.0,0.0,0.0,50.0" in lines
    assert "2027-01-15,domain,(none),1,0.0,0.0,100.0,0.0,0.0" in lines
