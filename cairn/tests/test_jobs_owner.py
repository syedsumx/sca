"""Scheduled jobs (L57) without a container: who a changed job runs as, and what its recipients are told."""

from __future__ import annotations

from cairn.common.db import get_engine, init_db
from cairn.pipelines.jobs import Jobs

ALICE, BOB, OPS = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:ops"


class Runner:
    def __init__(self):
        self.envs = []

    def run(self, run_id, kind, files, env, timeout_s):
        self.envs.append(env)
        return 1, "Traceback\nValueError: customer 42 owes 1,234.56\n=====CAIRN-JOB-OUTPUT=====\n"


def _svc(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/j.db")
    init_db(engine)
    told = []
    s = Jobs(engine, Runner(), lambda a: a in {ALICE, OPS}, lambda a: a == OPS, lambda owner, ttl: f"tok-{owner}",
             "https://trino.example:8443", lambda *a: told.append(a))
    return s, told


def test_an_operator_who_changes_a_job_owns_it_and_it_runs_as_them(tmp_path):
    s, _ = _svc(tmp_path)
    s.save(ALICE, {"name": "nightly", "kind": "python", "code": "print(1)"})
    s.save(OPS, {"kind": "python", "code": "print('as whom?')"}, "nightly")
    assert s.get(OPS, "nightly")["owner"] == OPS
    s.run_now(OPS, "nightly", wait=True)
    assert s.runner.envs[-1]["CAIRN_USER"] == "ops" and s.runner.envs[-1]["CAIRN_TRINO_TOKEN"] == f"tok-{OPS}"


def test_recipients_are_not_told_the_owners_log(tmp_path):
    s, told = _svc(tmp_path)
    s.save(ALICE, {"name": "breaks", "kind": "python", "code": "x", "recipients": [BOB]})
    s.run_now(ALICE, "breaks", wait=True)
    by = {tuple(who): body for _, _, body, who, _ in told}
    assert "customer 42" in by[(ALICE,)]
    assert "customer 42" not in by[(BOB,)] and by[(BOB,)] == "The run failed."
