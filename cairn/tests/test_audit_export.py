"""Audit log export: chain order, resumable cursor, offline verification, API and CLI."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.audit import AuditLog, export_lines, verify_entries
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ALICE, BOB = "urn:li:corpuser:alice", "urn:li:corpuser:bob"


def _log(engine, n=5):
    audit = AuditLog(engine)
    for i in range(n):
        audit.record("governance", f"action.{i}", ALICE, f"urn:li:dataset:{i}", {"i": i})
    return audit


def test_export_is_in_chain_order_and_verifies_offline(engine):
    audit = _log(engine)
    rows = list(audit.export(batch=2))                       # several pages
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows) and len(rows) == 5
    lines = list(export_lines(rows))
    parsed = [json.loads(line) for line in lines]
    result = verify_entries(parsed)
    assert result["valid"] and result["entries_checked"] == 5 and result["head"] == audit.verify_chain()["head"]


def test_resume_and_continuity_between_exports(engine):
    audit = _log(engine, 3)
    first = list(audit.export())
    audit.record("governance", "later", BOB)
    second = list(audit.export(after_seq=first[-1]["seq"]))
    assert [r["action"] for r in second] == ["later"]
    v1, v2 = verify_entries(first), verify_entries(second)
    assert v1["valid"] and v2["valid"] and v2["starts_after"] == v1["head"]   # nothing missing between


def test_tampering_and_gaps_are_detected(engine):
    rows = list(_log(engine).export())
    edited = [dict(r) for r in rows]
    edited[2]["actor"] = "urn:li:corpuser:mallory"
    assert verify_entries(edited) == {"valid": False, "broken_at_seq": rows[2]["seq"], "entries_checked": 2,
                                      "reason": "entry content does not match its hash"}
    gap = rows[:2] + rows[3:]
    assert not verify_entries(gap)["valid"] and "gap" in verify_entries(gap)["reason"]


def test_filters_and_csv(engine):
    audit = _log(engine, 2)
    audit.record("scim", "user.create", "scim")
    assert [r["category"] for r in audit.export(category="scim")] == ["scim"]
    assert list(audit.export(since="2999-01-01")) == []
    text = "".join(export_lines(audit.export(), "csv"))
    table = list(csv.DictReader(io.StringIO(text)))
    assert len(table) == 3 and json.loads(table[0]["details"]) == {"i": 0}
    assert "".join(export_lines([], "csv")).startswith("seq,id,ts")


def test_api_export_requires_policy_admin(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="alice-all", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ALICE})]))
    s.audit.record("governance", "x", ALICE)
    c = TestClient(create_api_app(s))
    assert c.get("/governance/audit/export", headers={"X-Cairn-Actor": BOB}).status_code == 403
    r = c.get("/governance/audit/export", headers={"X-Cairn-Actor": ALICE})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    entries = [json.loads(line) for line in r.text.splitlines()]
    assert verify_entries(entries)["valid"] and entries
    r = c.get("/governance/audit/export?format=csv&category=governance", headers={"X-Cairn-Actor": ALICE})
    assert r.text.startswith("seq,id,ts")


def test_cli_export_and_verify_file(engine, tmp_path, settings):
    _log(engine, 4)
    env = {**os.environ, "CAIRN_DATABASE_URL": settings.database_url}
    out = tmp_path / "audit.jsonl"
    cli = [sys.executable, "-m", "cairn.cli"]
    subprocess.run(cli + ["audit", "export", "--out", str(out)], check=True, env=env, timeout=60)
    assert len(out.read_text().splitlines()) == 4
    ok = subprocess.run(cli + ["audit", "verify-file", str(out)], env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["entries_checked"] == 4
    lines = out.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["action"] = "edited"
    out.write_text("\n".join([lines[0], json.dumps(tampered), *lines[2:]]) + "\n")
    bad = subprocess.run(cli + ["audit", "verify-file", str(out)], env=env, capture_output=True, text=True)
    assert bad.returncode == 1 and not json.loads(bad.stdout)["valid"]
