"""The shipped policy bundle meets its decision tests (config/policy-tests), and the runner catches regressions."""

from __future__ import annotations

import pathlib

from cairn.cli import run_policy_tests

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_baseline_bundle_decisions():
    results = run_policy_tests(str(ROOT / "config" / "policies"), str(ROOT / "config" / "policy-tests"))
    assert results and [r for r in results if not r["ok"]] == []


def test_runner_reports_a_changed_decision(tmp_path):
    case = tmp_path / "t.yaml"
    case.write_text("""
cases:
  - name: wrong expectation
    request: {actor: "urn:li:corpuser:x", privilege: EDIT_ENTITY_TAGS,
              resource: {type: dataset, urn: "urn:li:dataset:(urn:li:dataPlatform:x,y,PROD)"}}
    expect: ALLOW
""")
    [result] = run_policy_tests(str(ROOT / "config" / "policies"), str(case))
    assert not result["ok"] and "expected ALLOW" in result["problems"][0]
