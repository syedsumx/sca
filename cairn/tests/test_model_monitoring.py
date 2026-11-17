"""Model monitoring (cairn.ml.monitoring) without MLflow: what is kept of a model's calls, who reads it, and the
checks' arithmetic. The end-to-end tests with MLflow's real servers are test_ml_e2e.py and
test_model_monitoring_e2e.py."""

import json
import time
from types import SimpleNamespace

from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from cairn.ml.mlflow_gateway import MlflowGateway, ml_models
from cairn.ml.monitoring import ModelMonitoring, ml_inferences, ml_monitor_runs, ml_monitors

ANA, MAL = "urn:li:corpuser:ana", "urn:li:corpuser:mallory"


def _p(urn):
    return SimpleNamespace(urn=urn, is_service=False)


def _setup(**kw):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for t in (ml_models, ml_monitors, ml_inferences, ml_monitor_runs):
        t.create(engine)
    gw = MlflowGateway("http://mlflow.invalid", engine, None, None, None, lambda p, priv, urn: False)
    mon = ModelMonitoring(engine, gw.may_view, gw.may_manage, gw.owner, **kw)
    gw.monitoring = mon
    with engine.begin() as conn:
        conn.execute(insert(ml_models).values(name="churn", owner=ANA, created_s=0))
    mon.configure(_p(ANA), "churn", {"baseline": {"since_ms": 0, "until_ms": 1}})
    return engine, gw, mon


def _call(mon, model="churn", rows=({"age": 41, "email": "jane@example.com"},), preds=(1,)):
    return mon.record(model, ANA, 200, 12, json.dumps({"dataframe_records": list(rows)}).encode(),
                      json.dumps({"predictions": list(preds)}).encode())


def _baseline_until_now(mon, model="churn", **kw):
    """The calls logged so far as the baseline (a reference period that is over)."""
    until = int(time.time() * 1000) + 1
    time.sleep(0.005)
    mon.configure(_p(ANA), model, {"baseline": {"since_ms": 0, "until_ms": until}, **kw})


def _own(engine, name, owner):
    with engine.begin() as conn:
        conn.execute(insert(ml_models).values(name=name, owner=owner, created_s=0))


def test_a_deleted_models_calls_do_not_go_to_whoever_registers_its_name_next():
    """Deleting a model frees its name: the next person to register it owns it, and a model's managers read its
    logged calls, with what they were given. Those calls, its monitor and checks go with the model."""
    engine, gw, mon = _setup()
    _call(mon)
    assert mon.inferences(_p(ANA), "churn")[0]["inputs"] == [{"age": 41, "email": "jane@example.com"}]
    gw._changed(_p(ANA), "registered-models", "delete", "churn", {})
    _own(engine, "churn", MAL)                                       # Mallory registers the free name
    assert mon.inferences(_p(MAL), "churn") == []
    assert mon.monitor("churn") is None and mon.checks(_p(MAL), "churn") == []


def test_a_training_table_is_the_baseline_only_for_who_may_read_it():
    """Cairn reads a baseline table with its own catalog client, unmasked; the profile's means and drift come back
    to whoever may see the model. So setting one up is a read of the table by the model's manager: refused without
    read access, under a row filter, or with a baseline feature masked for them."""
    from cairn.coreproxy.masking import DatasetPlan
    from cairn.ml.monitoring import MonitorError, read_check

    reads, decisions = [], {}

    def read_table(table, columns):
        reads.append(table)
        return [{"salary": 50_000 + i, "grade": "A"} for i in range(200)]

    def decide(actor, urn):
        assert urn == "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
        return decisions.get(actor, (False, False, None))
    engine, gw, mon = _setup(read_table=read_table)
    mon.may_read_table = read_check(decide, lambda t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)")
    body = {"baseline": {"table": "hr.salaries"}, "features": ["salary"]}
    for decision, msg in [((False, False, None), "needs read access"), ((True, True, None), "row filter"),
                          ((True, False, DatasetPlan(fields={"salary"}, mask_queries=True)), "salary is masked")]:
        decisions[ANA] = decision
        try:
            mon.configure(_p(ANA), "churn", body)
            raise AssertionError("set up")
        except MonitorError as e:
            assert e.status == 403 and msg in str(e), (decision, e)
    assert reads == []
    assert "table" not in mon.monitor("churn")["baseline"]                  # the earlier baseline is kept
    decisions[ANA] = (True, False, DatasetPlan(fields={"grade"}, mask_queries=True))
    try:
        mon.configure(_p(ANA), "churn", {"baseline": {"table": "hr.salaries"}})     # every column: grade is masked
        raise AssertionError("set up")
    except MonitorError as e:
        assert e.status == 403 and "grade is masked" in str(e)
    mon.configure(_p(ANA), "churn", body)
    assert reads == ["hr.salaries"]
    # Without a way to decide the read, a table baseline is refused.
    mon.may_read_table = None
    try:
        mon.configure(_p(ANA), "churn", body)
        raise AssertionError("set up")
    except MonitorError as e:
        assert e.status in (403, 503)


def test_nan_and_infinity_in_calls_or_labels_do_not_break_the_checks():
    """JSON as Python reads it takes NaN and Infinity; a check's metrics are served as strict JSON (FastAPI refuses
    NaN), so one such value in a window made GET .../monitor and .../checks fail for everyone."""
    engine, gw, mon = _setup()
    _call(mon, rows=[{"age": a} for a in range(100)], preds=[0.5] * 100)
    _baseline_until_now(mon, min_rows=1)
    body = b'{"dataframe_records": [{"age": NaN}, {"age": Infinity}, {"age": 30}]}'
    iid = mon.record("churn", ANA, 200, 9, body, b'{"predictions": [0.5, 1.5, 2.5]}')
    mon.feedback(_p(ANA), "churn", {"inference_id": iid, "labels": [float("nan"), 1.0, 2.0]})
    run = mon.check("churn")
    json.dumps(run, allow_nan=False)
    json.dumps(mon.get(_p(ANA), "churn"), allow_nan=False)
    json.dumps(mon.inferences(_p(ANA), "churn"), allow_nan=False)
    age = run["metrics"]["features"]["age"]
    assert age["mean"] == round((sum(range(100)) + 30) / 101, 6)            # NaN and Infinity counted as missing
    assert age["null_share"] == round(2 / 103, 4) and run["metrics"]["mae"] == 0.5 and run["metrics"]["labelled_rows"] == 2


def test_a_renamed_models_calls_move_with_it():
    engine, gw, mon = _setup()
    _call(mon)
    _baseline_until_now(mon)
    mon.check("churn")
    gw._changed(_p(ANA), "registered-models", "rename", "churn", {"new_name": "churn_v2"})
    _own(engine, "churn", MAL)
    assert mon.inferences(_p(MAL), "churn") == [] and mon.monitor("churn") is None
    assert mon.inferences(_p(ANA), "churn_v2")[0]["inputs"][0]["age"] == 41
    assert mon.monitor("churn_v2") is not None and len(mon.checks(_p(ANA), "churn_v2")) == 1
    with engine.connect() as conn:
        assert {r.model for r in conn.execute(select(ml_inferences.c.model))} == {"churn_v2"}
