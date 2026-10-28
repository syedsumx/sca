"""The load-test runner's limit checks (the run itself needs a live stack)."""

import importlib.util
import os

spec = importlib.util.spec_from_file_location("loadtest", os.path.join(os.path.dirname(__file__), "load", "loadtest.py"))
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


def test_check_reports_each_broken_limit():
    results = {"scenarios": {"pdp_authorize": {"p95_ms": 30.0, "rps": 400, "error_rate": 0.0},
                             "enrich": {"p95_ms": 900.0, "rps": 20, "error_rate": 0.05}}}
    limits = {"pdp_authorize": {"p95_ms": 50, "min_rps": 200}, "enrich": {"p95_ms": 500, "min_rps": 30}}
    assert loadtest.check(results, limits) == ["enrich: p95 900.0 ms > 500 ms", "enrich: 20 req/s < 30",
                                               "enrich: error rate 5.00% > 1.00%"]
    assert loadtest._pct([5, 1, 9, 3], 0.5) == 5 and loadtest._pct([], 0.9) == 0.0
