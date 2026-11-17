"""Scheduled jobs (L57) without Docker: who a run runs as and when, with a runtime that records what it was asked
to run. The containers themselves are covered by tests/test_jobs.py (Docker and the cairn/jobs image)."""

from __future__ import annotations

import pytest

from cairn.common.db import get_engine, init_db
from cairn.pipelines.jobs import JobError, Jobs

ALICE, OPS = "urn:li:corpuser:alice", "urn:li:corpuser:ops"


class FakeRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, run_id, kind, files, env, timeout_s):
        self.calls.append({"run": run_id, "env": dict(env)})
        return 0, "log\n=====CAIRN-JOB-OUTPUT=====\nout\n"


@pytest.fixture
def svc(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/j.db")
    init_db(engine)
    minted, told = [], []

    def mint(owner, ttl):
        minted.append(owner)
        return "tok"
    s = Jobs(engine, FakeRunner(), lambda a: True, lambda a: a == OPS, mint, "https://trino", lambda *a: told.append(a))
    s.minted, s.told = minted, told
    return s


def test_a_suspended_owners_jobs_stop_running(svc):
    suspended: set[str] = set()
    svc.active = lambda owner: f"{owner} is suspended" if owner in suspended else None
    svc.save(ALICE, {"name": "nightly", "kind": "python", "code": "print(1)", "cron": "0 2 * * *"})
    assert svc.run_now(ALICE, "nightly", wait=True)["status"] == "succeeded"
    suspended.add(ALICE)                                    # deactivated (People, SCIM) after defining the job
    due = svc.get(OPS, "nightly")["next_run_ms"]
    assert svc.run_due(now=due, wait=True) == 1
    run = svc.runs(OPS, "nightly")[0]
    assert run["status"] == "failed" and "suspended" in run["error"]
    assert len(svc.runner.calls) == 1 and svc.minted == [ALICE]   # nothing ran and no token was minted for them
    with pytest.raises(JobError, match="suspended"):        # an operator's manual run is refused the same way
        svc.contained(ALICE, "python", "print(1)", {}, 60, "nightly", "r1")


def test_an_owner_whose_state_cannot_be_checked_does_not_run(svc):
    def broken(owner):
        raise RuntimeError("Cairn Core is down")
    svc.active = broken
    svc.save(ALICE, {"name": "nightly", "kind": "python", "code": "print(1)"})
    r = svc.run_now(ALICE, "nightly", wait=True)
    assert r["status"] == "failed" and svc.runner.calls == [] and svc.minted == []


def test_a_scheduled_run_does_not_start_while_the_last_one_still_runs(svc):
    import threading

    release = threading.Event()
    real = svc.runner.run

    def slow(*a):
        release.wait(10)
        return real(*a)
    svc.runner.run = slow
    svc.save(ALICE, {"name": "minutely", "kind": "python", "code": "print(1)", "cron": "* * * * *"})
    first = svc.get(ALICE, "minutely")["next_run_ms"]
    assert svc.run_due(now=first) == 1                      # started; still running a minute later
    assert svc.run_due(now=first + 60_000) == 0             # skipped, not a second copy beside it
    assert [r["status"] for r in svc.runs(ALICE, "minutely")] == ["running"]
    assert svc.get(ALICE, "minutely")["next_run_ms"] == first + 120_000
    release.set()
    svc.pool.shutdown(wait=True)
    assert [r["status"] for r in svc.runs(ALICE, "minutely")] == ["succeeded"]


def test_a_run_that_floods_its_output_is_kept_within_the_limit(tmp_path, monkeypatch):
    # Whatever the container prints (here 20 MB, as code writing to the shell's own stdout through /proc would),
    # Cairn keeps at most the limit in memory: the log's end and the output's start, then reads and drops the rest.
    from cairn.pipelines.jobs import MARKER, DockerRunner

    monkeypatch.setenv("CAIRN_JOBS_MAX_OUTPUT_BYTES", "1000")
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\necho start; echo '{MARKER}'; head -c 20000000 /dev/zero; exit 3\n")
    docker.chmod(0o755)
    rc, text = DockerRunner("img", docker=str(docker)).run("r1", "python", {"job.py": "pass"}, {"A": "b"}, 60)
    assert rc == 3 and text.startswith("start")
    assert len(text) <= 2 * 1000 + 65536


def test_the_container_keeps_its_log_and_output_within_the_limit(monkeypatch):
    from cairn.pipelines.jobs import script

    monkeypatch.setenv("CAIRN_JOBS_MAX_OUTPUT_BYTES", "1000")
    s = script("python", 60)
    assert "tail -c 1000 /tmp/log" in s and "head -c 1000 /tmp/out.txt" in s
