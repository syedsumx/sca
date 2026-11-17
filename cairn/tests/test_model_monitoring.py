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
