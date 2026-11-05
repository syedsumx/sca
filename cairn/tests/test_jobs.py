"""Scheduled notebook and Python jobs (L57), run for real in the cairn/jobs image: the executed notebook kept, a
failure told, the time limit kept, the owner's token never on a command line, and RUN_JOBS checked at each run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi import Request

from cairn.common.db import get_engine, init_db
from cairn.pipelines import jobs as jobs_mod
from cairn.pipelines.jobs import DockerRunner, JobError, Jobs

IMAGE = os.environ.get("CAIRN_JOBS_TEST_IMAGE", "cairn/jobs:dev")
ALICE, BOB, OPS = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:ops"


def _image_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_ready(), reason=f"needs Docker and the {IMAGE} image")


def notebook(*sources: str) -> dict:
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                                                           "language": "python"}},
            "cells": [{"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": s}
                      for s in sources]}


@pytest.fixture
def svc(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/j.db")
    init_db(engine)
    told, minted, allowed = [], [], {ALICE, OPS}

    def mint(owner, ttl):
        minted.append((owner, ttl))
        return f"tok-{owner.rsplit(':', 1)[1]}-secret"
    s = Jobs(engine, DockerRunner(IMAGE, memory="512m", cpus="1"), lambda a: a in allowed, lambda a: a == OPS,
             mint, "https://trino.example:8443", lambda *a: told.append(a))
    s.told, s.minted, s.allowed_set = told, minted, allowed
    return s


def test_a_notebook_runs_as_its_owner_and_its_output_is_kept(svc):
    nb = notebook("import os\nprint('hello', os.environ['CAIRN_USER'], PARAMS['region'])",
                  "print('token set', bool(os.environ.get('CAIRN_TRINO_TOKEN')), os.environ['CAIRN_TRINO_URL'])\n"
                  "print('uid', os.getuid())")
    svc.save(ALICE, {"name": "daily", "kind": "notebook", "notebook": nb, "params": {"region": "emea"},
                     "cron": "0 2 * * *"})
    r = svc.run_now(ALICE, "daily", wait=True)
    assert r["status"] == "succeeded", r
    run = svc.run_detail(ALICE, "daily", r["run"])
    out = json.loads(run["output"])                                  # the executed notebook
    texts = ["".join(o.get("text", "")) for c in out["cells"] for o in c.get("outputs", [])]
    assert "hello alice emea\n" in texts
    assert "token set True https://trino.example:8443\nuid 1000\n" in texts
    assert out["cells"][0]["metadata"]["tags"] == ["injected-parameters"]
    assert run["as_user"] == ALICE and run["trigger"] == "manual"
    assert svc.minted == [(ALICE, 3600 + 300)]
    assert svc.told == []                                            # notify: failure, and it succeeded
    job = svc.get(ALICE, "daily")
    assert job["next_run_ms"] and "source" not in job


def test_a_failing_script_is_told_with_its_last_line(svc):
    svc.save(ALICE, {"name": "breaks", "kind": "python", "code": "print('starting')\nraise SystemExit('no rows today')",
                     "recipients": [BOB]})
    r = svc.run_now(ALICE, "breaks", wait=True)
    assert r["status"] == "failed" and "no rows today" in r["error"]
    run = svc.run_detail(ALICE, "breaks", r["run"])
    assert run["output"].strip() == "starting" and "no rows today" in run["log"]
    (kind, title, body, who, payload), = svc.told
    assert kind == "job.failed" and who == [ALICE, BOB] and payload == {"job": "breaks", "run": r["run"]}


def test_the_container_is_locked_down(svc):
    code = ("import os, pathlib\n"
            "try:\n    pathlib.Path('/etc/x').write_text('x'); print('rootfs writable')\n"
            "except OSError:\n    print('rootfs read-only')\n"
            "try:\n    pathlib.Path('/work/job.py').write_text('x'); print('code writable')\n"
            "except OSError:\n    print('code read-only')\n"
            "print('caps', open('/proc/self/status').read().split('CapEff:')[1].split()[0])\n"
            "print('nnp', open('/proc/self/status').read().split('NoNewPrivs:')[1].split()[0])\n")
    svc.save(ALICE, {"name": "probe", "kind": "python", "code": code})
    r = svc.run_now(ALICE, "probe", wait=True)
    out = svc.run_detail(ALICE, "probe", r["run"])["output"]
    assert "rootfs read-only" in out and "code read-only" in out
    assert "caps 0000000000000000" in out and "nnp 1" in out


def test_the_token_never_reaches_a_command_line(svc, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(jobs_mod.subprocess, "run", spy)
    svc.save(ALICE, {"name": "quiet", "kind": "python", "code": "import os; print(len(os.environ['CAIRN_TRINO_TOKEN']))"})
    r = svc.run_now(ALICE, "quiet", wait=True)
    assert r["status"] == "succeeded"
    assert seen and not any("tok-alice-secret" in " ".join(c) for c in seen)
    out = svc.run_detail(ALICE, "quiet", r["run"])
    assert out["output"].strip() == str(len("tok-alice-secret")) and "tok-alice-secret" not in (out["log"] or "")


def test_a_run_over_its_time_limit_is_stopped(svc, monkeypatch):
    monkeypatch.setattr(jobs_mod, "script", lambda kind, t: "sleep 30")     # longer than the shortened limit below
    monkeypatch.setattr(DockerRunner, "run", _with_limit(DockerRunner.run, 3))
    svc.save(ALICE, {"name": "slow", "kind": "python", "code": "pass", "timeout_minutes": 1})
    r = svc.run_now(ALICE, "slow", wait=True)
    assert r["status"] == "failed" and "time limit" in r["error"]
    names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert f"cairn-job-{r['run']}" not in names                       # the container is gone


def _with_limit(run, seconds):
    def limited(self, run_id, kind, files, env, timeout_s):
        orig = jobs_mod.subprocess.run

        def short(cmd, *a, **k):
            if k.get("timeout") and cmd[:2] == [self.docker, "run"]:
                k["timeout"] = seconds
            return orig(cmd, *a, **k)
        jobs_mod.subprocess.run = short
        try:
            return run(self, run_id, kind, files, env, timeout_s)
        finally:
            jobs_mod.subprocess.run = orig
    return limited


def test_rights_are_checked_when_saved_and_at_each_run(svc):
    with pytest.raises(JobError) as e:
        svc.save(BOB, {"name": "mine", "kind": "python", "code": "pass"})
    assert e.value.status == 403
    svc.save(ALICE, {"name": "owned", "kind": "python", "code": "print(1)", "cron": "* * * * *"})
    with pytest.raises(JobError) as e:                               # others don't see it
        svc.get(BOB, "owned")
    assert e.value.status == 404
    assert svc.get(OPS, "owned")["owner"] == ALICE                   # operators do
    svc.allowed_set.discard(ALICE)                                   # the owner loses RUN_JOBS
    assert svc.run_due(now=svc.get(OPS, "owned")["next_run_ms"], wait=True) == 1
    (run,) = svc.runs(OPS, "owned")
    assert run["status"] == "failed" and "RUN_JOBS" in run["error"] and run["trigger"] == "schedule"
    assert svc.minted == []                                          # no token was minted for them
    assert svc.told[0][0] == "job.failed"


def test_a_scheduled_run_is_claimed_once(svc):
    svc.save(ALICE, {"name": "tick", "kind": "python", "code": "print(1)", "cron": "*/5 * * * *"})
    due = svc.get(ALICE, "tick")["next_run_ms"]
    assert svc.run_due(now=due - 1, wait=True) == 0
    assert svc.run_due(now=due, wait=True) == 1
    assert svc.run_due(now=due, wait=True) == 0                      # already claimed; next is five minutes on
    assert svc.get(ALICE, "tick")["next_run_ms"] == due + 300_000


def test_bad_definitions_are_refused(svc):
    for body, msg in [({"kind": "sh", "code": "x"}, "kind"), ({"kind": "notebook", "notebook": {"cells": []}}, "notebook"),
                      ({"kind": "python", "code": "x", "cron": "every day"}, "cron"),
                      ({"kind": "python", "code": "x", "timeout_minutes": 10_000}, "timeout_minutes"),
                      ({"kind": "python", "code": "x", "recipients": ["bob"]}, "recipients"),
                      ({"name": "Bad Name", "kind": "python", "code": "x"}, "name")]:
        with pytest.raises(JobError, match=msg):
            svc.save(ALICE, {"name": "ok-name", **body})


def test_no_runtime_means_jobs_are_refused(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/n.db")
    init_db(engine)
    with pytest.raises(JobError) as e:
        Jobs(engine, None, lambda a: True, lambda a: False).save(ALICE, {"name": "x", "kind": "python", "code": "x"})
    assert e.value.status == 503


def test_the_api(svc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.pipelines.jobs import build_jobs_router

    class P:
        def __init__(self, urn):
            self.urn = urn

    def principal(request: Request):
        return P(request.headers["x-user"])
    app = FastAPI()
    app.include_router(build_jobs_router(svc, principal))
    c = TestClient(app)
    a = {"x-user": ALICE}
    assert c.post("/v1/jobs", json={"name": "api", "kind": "python", "code": "print('hi')"}, headers=a).status_code == 201
    assert c.post("/v1/jobs", json={"name": "api", "kind": "python", "code": "x"}, headers=a).status_code == 409
    assert c.post("/v1/jobs", json={"name": "b", "kind": "python", "code": "x"}, headers={"x-user": BOB}).status_code == 403
    started = c.post("/v1/jobs/api/run", headers=a)                   # answered at once
    assert started.status_code == 202 and started.json()["status"] == "running"
    svc.pool.shutdown(wait=True)                                     # let it finish
    from concurrent.futures import ThreadPoolExecutor
    svc.pool = ThreadPoolExecutor(2)
    assert c.get(f"/v1/jobs/api/runs/{started.json()['run']}", headers=a).json()["status"] == "succeeded"
    r = c.post("/v1/jobs/api/run?wait=true", headers=a).json()
    assert r["status"] == "succeeded"
    assert [x["id"] for x in c.get("/v1/jobs/api/runs", headers=a).json()][0] == r["run"]
    assert c.get(f"/v1/jobs/api/runs/{r['run']}", headers=a).json()["output"].strip() == "hi"
    assert c.post("/v1/jobs/api/run?wait=true", headers={"x-user": BOB}).status_code == 403
    assert c.delete("/v1/jobs/api", headers={"x-user": BOB}).status_code == 403
    assert c.delete("/v1/jobs/api", headers=a).status_code == 204
    assert c.get("/v1/jobs", headers=a).json() == []


def test_a_run_lost_in_a_restart_is_failed_and_told(svc):
    from sqlalchemy import update

    from cairn.pipelines.jobs import job_runs

    svc.save(ALICE, {"name": "lost", "kind": "python", "code": "pass", "timeout_minutes": 5, "recipients": [BOB]})
    j = svc._get("lost")
    rid = svc._start(j, "schedule")                                  # started, then the process went away
    t0 = int(time.time() * 1000) - 3_600_000
    with svc.engine.begin() as conn:
        conn.execute(update(job_runs).where(job_runs.c.id == rid).values(started_ms=t0))
    svc.run_due(now=t0 + 5 * 60_000 + 900_000 - 1, wait=True)        # not yet past its limit and grace
    assert svc.run_detail(ALICE, "lost", rid)["status"] == "running"
    svc.run_due(now=t0 + 5 * 60_000 + 900_001, wait=True)
    run = svc.run_detail(ALICE, "lost", rid)
    assert run["status"] == "failed" and "lost" in run["error"]
    assert [t[0] for t in svc.told] == ["job.failed"] and svc.told[0][3] == [ALICE, BOB]
