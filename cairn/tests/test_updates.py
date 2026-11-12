"""Scheduled updates (L160): the policy (windows in a time zone, manual or automatic, approval, four eyes), plans
made only from a feed that verifies now, one open plan at a time, the scheduler starting a plan only inside its
window, and what happens when the updater cannot start or dies without reporting. The feed is real (signed,
served over HTTP); the updater Job itself runs on Kubernetes in tests/test_updates_kind_e2e.py."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

from cairn.common.db import metadata
from cairn.release import publish as pub
from cairn.release.feed import ReleaseFeed, state_table
from cairn.release.runner import StepFailed, check_images, digests_of
from cairn.release.updates import (UpdateError, UpdateManager, check_policy, next_window, plans_table, policy_table,
                                   window_open)
from test_release_feed import keypair, manifest, served  # noqa: F401 - the served fixture

sys.path.insert(0, str(Path(__file__).parent))
OPS, OPS2 = "urn:li:corpuser:ops", "urn:li:corpuser:ops2"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_windows_in_a_time_zone_and_past_midnight():
    p = check_policy({"windows": [{"days": ["sat"], "start": "22:00", "hours": 4}], "timezone": "Europe/Berlin"})
    # Saturday 22:00 in Berlin (winter, UTC+1) is 21:00 UTC; the window runs to 02:00 on Sunday.
    assert not window_open(p, utc(2026, 11, 14, 20, 59))
    assert window_open(p, utc(2026, 11, 14, 21, 0))
    assert window_open(p, utc(2026, 11, 15, 0, 30))            # Sunday 01:30 Berlin: still Saturday's window
    assert not window_open(p, utc(2026, 11, 15, 1, 0))
    assert next_window(p, utc(2026, 11, 15, 1, 0)) == utc(2026, 11, 21, 21, 0)
    anytime = check_policy({})
    assert window_open(anytime, utc(2026, 11, 11, 13, 0))
    for bad, why in (({"mode": "sometimes"}, "manual or auto"), ({"timezone": "Mars/Base"}, "IANA"),
                     ({"windows": [{"days": ["sat"], "start": "25:00", "hours": 2}]}, "HH:MM"),
                     ({"windows": [{"days": ["sat"], "start": "02:00", "hours": 30}]}, "1 to 24"),
                     ({"auto_severities": ["urgent"]}, "auto_severities"), ({"secret": 1}, "unknown")):
        with pytest.raises(UpdateError, match=why):
            check_policy(bad)


class Launcher:
    """Stands in for the Kubernetes Job API here (tests/test_updates_kind_e2e.py starts real Jobs)."""

    def __init__(self, fail=False):
        self.started, self.jobs, self.fail = [], {}, fail

    def start(self, plan_id):
        if self.fail:
            raise RuntimeError("jobs.batch is forbidden")
        self.started.append(plan_id)
        self.jobs[f"job-{plan_id}"] = "running"
        return f"job-{plan_id}"

    def state(self, name):
        return self.jobs.get(name, "missing")


@pytest.fixture()
def world(served, tmp_path):  # noqa: F811
    feed_dir, url = served
    signer, keys = keypair("rel-1")
    for m in (manifest("2.0.0", "1.0.0"), manifest("2.1.0", "2.0.0", severity="security")):
        pub.publish(feed_dir, pub.sign_manifest(m, [signer]), [signer])
    e = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/u.db")
    metadata.create_all(e, tables=[state_table, policy_table, plans_table])
    feed = ReleaseFeed(e, url, "stable", "1.0.0", keys=keys, dev_keys={})
    return feed_dir, signer, feed, e


def test_a_plan_waits_for_approval_and_its_window(world):
    feed_dir, signer, feed, e = world
    launcher = Launcher()
    m = UpdateManager(e, feed, launcher=launcher)
    m.set_policy(OPS, {"windows": [{"days": ["sat"], "start": "02:00", "hours": 4}], "separate_approver": True})
    p = m.plan(OPS)
    assert p["status"] == "pending_approval" and p["path"] == ["2.0.0", "2.1.0"] and p["severity"] == "security"
    with pytest.raises(UpdateError, match="finish or cancel"):
        m.plan(OPS2)                                                      # one open plan at a time
    with pytest.raises(UpdateError, match="someone other"):
        m.approve(OPS, p["id"])                                           # four eyes
    assert m.approve(OPS2, p["id"])["status"] == "scheduled"
    assert m.tick(utc(2026, 11, 11, 12, 0)) is None and launcher.started == []      # a Wednesday: no window
    assert m.tick(utc(2026, 11, 14, 3, 0)) == f"started {p['id']}"                   # Saturday 03:00 UTC
    got = m.get(p["id"])
    assert got["status"] == "running" and got["job"] == f"job-{p['id']}"
    m.finish(p["id"], "succeeded")
    assert m.get(p["id"])["status"] == "succeeded"

    # To one version only, not before a time; cancelled before it starts.
    q = m.plan(OPS, version="2.0.0", not_before="2026-11-21T03:30:00Z")
    assert q["path"] == ["2.0.0"]
    m.approve(OPS2, q["id"])
    assert m.tick(utc(2026, 11, 21, 3, 0)) is None                       # in the window, before not_before
    assert m.cancel(OPS, q["id"])["status"] == "cancelled"
    with pytest.raises(UpdateError, match="not on the way up"):
        m.plan(OPS, version="9.9.9")


def test_nothing_is_planned_from_a_feed_that_does_not_verify(world):
    feed_dir, signer, feed, e = world
    m = UpdateManager(e, feed, launcher=Launcher())
    (feed_dir / "releases/2.1.0.json").write_text("{}")
    with pytest.raises(UpdateError, match="did not verify"):
        m.plan(OPS)


def test_automatic_updates_take_only_the_severities_set(world):
    feed_dir, signer, feed, e = world
    launcher = Launcher()
    m = UpdateManager(e, feed, launcher=launcher)
    m.set_policy(OPS, {"mode": "auto", "auto_severities": ["critical"], "require_approval": False})
    assert m.tick(utc(2026, 11, 11, 12, 0)) is None                      # the newest is only "security"
    assert m.plans() == []
    m.set_policy(OPS, {"auto_severities": ["security", "critical"]})
    m._last_auto_check = 0
    did = m.tick(utc(2026, 11, 11, 12, 0))
    (plan,) = m.plans()
    assert plan["auto"] and plan["requested_by"].endswith("__cairn_updates__") and did == f"started {plan['id']}"
    # It died without reporting: the plan says so, and the same release is not planned again by itself.
    launcher.jobs.clear()
    assert m.tick(utc(2026, 11, 11, 12, 5)) == f"failed {plan['id']}"
    assert "without reporting" in m.get(plan["id"])["error"]
    m._last_auto_check = 0
    m.tick(utc(2026, 11, 11, 13, 0))
    assert len(m.plans()) == 1


def test_a_job_that_ended_without_reporting_fails_its_plan(world):
    feed_dir, signer, feed, e = world
    launcher = Launcher()
    m = UpdateManager(e, feed, launcher=launcher)
    m.set_policy(OPS, {"require_approval": False})
    p = m.plan(OPS)
    m.tick(utc(2026, 11, 11, 12, 0))
    launcher.jobs[f"job-{p['id']}"] = "succeeded"            # the Job finished, the plan was never finished
    assert m.tick(utc(2026, 11, 11, 12, 5)) == f"failed {p['id']}"
    assert m.get(p["id"])["status"] == "failed"


def test_job_names_are_dns_labels():
    from cairn.release.runner import JobLauncher

    long = JobLauncher("x" * 50 + "-cairn-updater", "ns", api="http://127.0.0.1:1", token="t", http=object())
    calls = []
    long._call = lambda method, path, body=None: calls.append(body) or {"spec": {"jobTemplate": {"spec": {
        "template": {"spec": {"containers": [{"name": "u"}]}}}}}}
    name = long.start("upd_0123456789abcdef")
    assert len(name) <= 63 and not name.endswith("-") and "--" not in name and name.endswith("-0123456789ab")
    env = calls[-1]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [{"name": "CAIRN_UPDATE_PLAN", "value": "upd_0123456789abcdef"}]
    assert calls[-1]["spec"]["backoffLimit"] == 0


def test_an_updater_that_cannot_start_changes_nothing(world):
    feed_dir, signer, feed, e = world
    m = UpdateManager(e, feed, launcher=Launcher(fail=True))
    m.set_policy(OPS, {"require_approval": False})
    p = m.plan(OPS)
    assert m.tick(utc(2026, 11, 11, 12, 0)) == f"failed {p['id']}"
    got = m.get(p["id"])
    assert got["status"] == "failed" and "nothing was changed" in got["error"] and "forbidden" in got["error"]
    # Without an updater (Docker Compose, or the agent off): planned and shown, applied with the deployment's tools.
    m2 = UpdateManager(e, feed, launcher=None)
    p2 = m2.plan(OPS)
    m2.tick(utc(2026, 11, 11, 12, 0))
    assert m2.get(p2["id"])["status"] == "scheduled" and "no updater" in m2.get(p2["id"])["log"][-1]["msg"]


def test_images_must_be_the_signed_digests():
    signed = {"api": "registry.example.com/cairn/platform@sha256:" + "a" * 64}
    digests = digests_of(signed)
    assert digests == {"platform": "sha256:" + "a" * 64}
    good = "spec:\n  template:\n    spec:\n      containers:\n        - image: mirror.local/cairn/platform@sha256:" + \
           "a" * 64 + "\n        - image: postgres:16\n"
    check_images(good, digests)                                    # another registry (a mirror) is fine
    with pytest.raises(StepFailed, match="not the signed"):
        check_images(good.replace("@sha256:" + "a" * 64, ":2.0.0"), digests)
    with pytest.raises(StepFailed, match="none of the release"):
        check_images("spec:\n  containers:\n    - image: postgres:16\n", digests)


def test_the_api_for_operators(served, tmp_path):  # noqa: F811
    from iac_world import ANA_TOKEN, TOKEN, build

    from cairn.app import create_api_app

    feed_dir, url = served
    signer, keys = keypair("rel-1")
    pub.publish(feed_dir, pub.sign_manifest(manifest("9.0.0", "0.1.0"), [signer]), [signer])
    s = build(tmp_path, env={"CAIRN_RELEASE_FEED": url})
    s.release_feed.keys, s.release_feed.dev_keys = keys, {}
    assert s.release_updates is not None and s.release_updates.launcher is None
    http = TestClient(create_api_app(s))
    admin, ana = {"Authorization": f"Bearer {TOKEN}"}, {"Authorization": f"Bearer {ANA_TOKEN}"}
    assert http.put("/v1/system/updates/policy", json={"mode": "auto"}, headers=ana).status_code == 403
    pol = http.put("/v1/system/updates/policy", json={"timezone": "Asia/Kolkata", "windows": [
        {"days": ["sun"], "start": "01:00", "hours": 3}]}, headers=admin)
    assert pol.status_code == 200 and pol.json()["windows"][0]["days"] == ["sun"]
    assert http.put("/v1/system/updates/policy", json={"mode": "x"}, headers=admin).status_code == 400
    plan = http.post("/v1/system/updates/plans", json={}, headers=admin).json()
    assert plan["target"] == "9.0.0" and plan["status"] == "pending_approval"
    assert http.post(f"/v1/system/updates/plans/{plan['id']}/approve", headers=ana).status_code == 403
    assert http.post(f"/v1/system/updates/plans/{plan['id']}/approve", headers=admin).json()["status"] == "scheduled"
    over = http.get("/v1/system/updates/plans", headers=admin).json()
    assert over["updater"] is False and over["next_window"] and over["plans"][0]["id"] == plan["id"]
    assert http.post("/v1/system/updates/plans", json={}, headers=admin).status_code == 409
    assert http.post(f"/v1/system/updates/plans/{plan['id']}/cancel", headers=admin).json()["status"] == "cancelled"
    assert any(x["action"] == "updates.approved" for x in s.audit.about(f"urn:li:cairnUpdate:{plan['id']}"))
