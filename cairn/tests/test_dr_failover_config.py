"""`cairn dr watch` settings (queue row 24): refused when they could make the watcher act wrongly; the decision
rules on probes alone (the real databases and DNS server are in tests/test_dr_drill.py)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cairn.pipelines.dr_failover import Config, FailoverError, Watcher


@pytest.fixture
def raw(tmp_path):
    (tmp_path / "db").write_text("postgresql://u:p@db/x")
    (tmp_path / "tsig").write_text("c2VjcmV0")
    return {"state_file": str(tmp_path / "state.json"), "primary_checks": ["tcp://a:1", "https://a/health"],
            "standby_checks": ["http://b/health"], "standby_databases": [{"url_file": str(tmp_path / "db")}],
            "dns": {"server": "ns", "zone": "ex.com.", "key_name": "k.", "key_secret_file": str(tmp_path / "tsig"),
                    "records": [{"name": "api", "type": "A", "value": "192.0.2.2"}]}}


@pytest.mark.parametrize("change, message", [
    (lambda r: r.update(failures=1), "at least 2"),
    (lambda r: r.update(primary_checks=["ftp://a"]), "not an http"),
    (lambda r: r.update(standby_checks=[]), "standby_checks is required"),
    (lambda r: r["dns"].update(key_algorithm="hmac-md5"), "key_algorithm"),
    (lambda r: r["dns"]["records"][0].update(type="TXT"), "type"),
    (lambda r: r["dns"]["records"][0].update(ttl=86400), "ttl"),
    (lambda r: r["dns"].update(key_secret_file="/nonexistent/tsig"), "cannot read"),
    (lambda r: r["standby_databases"][0].update(url_file="/nonexistent/db"), "cannot read"),
])
def test_settings_that_could_misfire_are_refused(raw, change, message):
    change(raw)
    with pytest.raises(FailoverError, match=message):
        Config.parse(raw)


def test_only_a_whole_region_lost_with_a_healthy_standby_fails_over(raw, monkeypatch):
    up = {"tcp://a:1": True, "https://a/health": True, "http://b/health": True}
    w = Watcher(Config.parse({**raw, "standby_databases": []}), probe_fn=lambda u, t: up[u])
    calls = []
    monkeypatch.setattr(w, "fail_over", lambda: calls.append(1) or {"action": "failed-over"})
    assert w.tick()["why"] == "primary healthy"
    up["tcp://a:1"] = False
    assert [w.tick()["action"] for _ in range(5)] == ["none"] * 5          # one component down: not a lost region
    up["https://a/health"] = False
    assert [w.tick()["action"] for _ in range(2)] == ["none", "none"]
    up["tcp://a:1"] = up["https://a/health"] = True                         # back before the third round: count reset
    assert w.tick()["why"] == "primary healthy" and w.lost_ticks == 0
    up["tcp://a:1"] = up["https://a/health"] = False
    up["http://b/health"] = False
    assert [w.tick()["action"] for _ in range(3)] == ["none", "none", "refused"] and not calls
    up["http://b/health"] = True
    assert w.tick()["action"] == "failed-over" and calls == [1]


def test_the_cli_does_nothing_once_failed_over(raw, tmp_path):
    with open(raw["state_file"], "w") as f:
        json.dump({"failed_over_at": 1}, f)
    cfg = tmp_path / "failover.json"                                       # YAML is a superset of JSON
    cfg.write_text(json.dumps(raw))
    r = subprocess.run([sys.executable, "-m", "cairn.cli", "dr", "watch", "--config", str(cfg), "--once"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "already failed over" in r.stderr, r.stderr[-2000:]
    cfg.write_text(json.dumps({**raw, "failures": 1}))
    r = subprocess.run([sys.executable, "-m", "cairn.cli", "dr", "watch", "--config", str(cfg), "--once"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "at least 2" in r.stderr


def test_a_failed_round_is_retried_and_the_next_one_finishes(raw, monkeypatch):
    w = Watcher(Config.parse({**raw, "standby_databases": [], "interval_s": 0}), probe_fn=lambda u, t: u.startswith("http://b"))
    tries = []

    def flaky():
        tries.append(1)
        if len(tries) == 1:
            raise FailoverError("DNS update of api failed: Timeout")
        return {"action": "failed-over"}
    monkeypatch.setattr(w, "fail_over", flaky)
    assert w.run() == 0 and len(tries) == 2
