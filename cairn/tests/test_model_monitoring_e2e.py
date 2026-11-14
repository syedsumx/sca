"""Model monitoring against a training table in the lakehouse (the real Cairn Store and Cairn Catalog): the
baseline is read from the table once, calls far from it are drift, and a missing table or column is refused
when the monitor is set up. The calls through MLflow's real model server are in tests/test_ml_e2e.py."""

import json
import random

import pytest

pa = pytest.importorskip("pyarrow")

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: E402, F401  (fixtures)

ANA = "urn:li:corpuser:ana"


class Who:
    def __init__(self, urn):
        self.urn = urn


def test_payload_formats_and_psi():
    from cairn.ml.monitoring import predictions, profile, psi, records

    assert records({"dataframe_split": {"columns": ["a", "b"], "data": [[1, 2], [3, 4]]}}) == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert records({"dataframe_records": [{"a": 1}]}) == [{"a": 1}]
    assert records({"inputs": {"a": [1, 2], "b": [3, 4]}}) == [{"a": 1, "b": 3}, {"a": 2, "b": 4}]
    assert records({"instances": [[1, 2]]}) == [{"0": 1, "1": 2}] and records("x") is None
    assert predictions({"predictions": [1, 0]}) == [1, 0] and predictions([0.5]) == [0.5]
    rnd = random.Random(7)
    base = profile([rnd.gauss(50, 10) for _ in range(5000)])
    assert psi(base, [rnd.gauss(50, 10) for _ in range(2000)]) < 0.05           # the same population
    assert psi(base, [rnd.gauss(80, 10) for _ in range(2000)]) > 1               # shifted
    cats = profile(["EU"] * 70 + ["US"] * 30)
    assert psi(cats, ["EU"] * 68 + ["US"] * 32) < 0.01 and psi(cats, ["APAC"] * 50 + ["EU"] * 50) > 1
    assert psi(profile([None]), [1]) is None


@pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")
def test_a_training_table_is_the_baseline(lake, tmp_path):  # noqa: F811
    from pyiceberg.catalog.rest import RestCatalog
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.ml.monitoring import MonitorError, ModelMonitoring

    props = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
             "s3.path-style-access": "true"}
    cat = RestCatalog("test", **props)
    cat.create_namespace_if_not_exists("ml")
    rnd = random.Random(3)
    train = pa.table({"tenure": [rnd.randint(1, 40) for _ in range(500)],
                      "plan": [rnd.choice(["basic", "pro"]) for _ in range(500)],
                      "churned": [rnd.randint(0, 1) for _ in range(500)]})
    cat.create_table("ml.churn_training", schema=train.schema).append(train)
    reads = []

    def read_table(table, columns):
        reads.append(table)
        t = RestCatalog("cairn", **props).load_table(tuple(table.split(".")))
        return (t.scan(selected_fields=tuple(columns)) if columns else t.scan()).to_arrow().to_pylist()

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    sent = []
    mon = ModelMonitoring(engine, lambda p, m: True, lambda p, m: p.urn == ANA, lambda m: ANA, read_table,
                          lambda *a: sent.append(a))
    with pytest.raises(MonitorError, match="could not be read"):
        mon.configure(Who(ANA), "churn", {"baseline": {"table": "ml.nope"}})
    with pytest.raises(MonitorError, match="column: 'age'"):
        mon.configure(Who(ANA), "churn", {"baseline": {"table": "ml.churn_training"}, "features": ["tenure", "age"]})
    assert mon.monitor("churn") is None                                         # nothing half-made
    mon.configure(Who(ANA), "churn", {"baseline": {"table": "ml.churn_training"}, "features": ["tenure", "plan"],
                                      "min_rows": 50})
    n = len(reads)

    def calls(rows):
        for i in range(0, len(rows), 25):
            body = json.dumps({"dataframe_records": rows[i:i + 25]}).encode()
            mon.record("churn", ANA, 200, 12, body, json.dumps({"predictions": [0] * len(rows[i:i + 25])}).encode())
    calls([{"tenure": rnd.randint(1, 40), "plan": rnd.choice(["basic", "pro"])} for _ in range(200)])
    ok = mon.check("churn")
    assert ok["status"] == "ok" and ok["metrics"]["features"]["tenure"]["psi"] < 0.1, ok
    calls([{"tenure": rnd.randint(60, 90), "plan": "enterprise"} for _ in range(400)])
    drift = mon.check("churn")
    assert drift["status"] == "drift" and drift["metrics"]["drifted"] == ["tenure", "plan"], drift
    assert len(reads) == n                                       # the table was read once, when set up
    assert sent[0][0] == "ml.drift_detected" and sent[0][3] == [ANA]
