"""Managed Airflow's lifecycle rules, without an Airflow image: the service against a runtime that records what it is
asked and fails where a test says (the Docker and Kubernetes runtimes themselves run in tests/test_airflow_deployments
_e2e.py, _k8s.py and _kind.py). Real: the database (every table, its unique port index), secret scopes, workload
identity and the deployment keys.

* a deployment whose approval is required takes changes only through an approved promotion: direct DAG, git and image
  deploys are refused, and a rollback only goes back to a deploy that came in through one;
* a failed wake stops what it started, stays hibernating with the error, and the scheduler tries again with backoff;
* a failed restart, upgrade or image deploy is ``failed`` with the error and records what is running (a migrated
  database included); one refused before anything was touched leaves the deployment as it was;
* deletion waits for a change another request holds; the Docker runtime reports a volume still in use;
* two deployments made at once never get the same Docker port, even with no container bound to it yet;
* the pipeline user's grants go with its deployment, and a name in use is refused before its secrets are read;
* a manual hibernation during awake hours holds until the schedule's next sleep-to-wake;
* a DAG bundle's folders count towards its entries, and a bundle fits the API's body limit.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import tarfile

import pytest
from sqlalchemy import create_engine, insert, select

from cairn.airflow_deploy import bundle as bundles
from cairn.airflow_deploy.deployments import (APPROVAL_ONLY, LEASE_MS, WAKE_BACKOFF_MS, AirflowDeployments,
                                              DeploymentError, deploys_table)
from cairn.airflow_deploy.identity import Keys
from cairn.common.db import load_models, metadata
from cairn.databases.docker_runtime import RuntimeError_

ADMIN, ANA, RAJ = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:raj"
MONDAY_10 = int(dt.datetime(2026, 11, 16, 10, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
HOURS = {"awake": [{"days": "mon-fri", "from": "08:00", "to": "19:00"}], "timezone": "UTC", "wake_minutes": 30}


class Runtime:
    """A Docker-shaped runtime that does nothing but record, and fails at the step a test names."""
    name = "docker"
    executors = ("LocalExecutor",)
    default_executor = "LocalExecutor"

    def __init__(self):
        self.images = {"3.3.2": "cairn/airflow:0.1.0", "3.3.9": "cairn/airflow:0.2.0"}
        self.calls: list[tuple] = []
        self.fail: dict[str, str] = {}         # step -> message ("wait_ready", "recreate:<stage>", "stop", "start")

    def _maybe(self, step):
        if step in self.fail:
            raise RuntimeError_(self.fail[step], 504)

    def image_for(self, version):
        if version not in self.images:
            raise RuntimeError_(f"Airflow {version} is not offered here", 400)
        return self.images[version]

    def allocate_port(self, exclude=()):
        p = 18080
        while p in set(exclude):
            p += 1
        self.calls.append(("allocate_port", p))
        return p

    def public_url(self, dep):
        return f"http://127.0.0.1:{dep['port']}"

    def api_url(self, dep):
        return self.public_url(dep)

    def create(self, dep, image):
        self.calls.append(("create", dep["name"], image))
        self._maybe("create")
        return {"image": image, "ops_user": 1}

    def wait_ready(self, dep, timeout=None):
        self.calls.append(("wait_ready", dep["name"]))
        self._maybe("wait_ready")
        return {}

    def recreate(self, dep, image, migrate=False, progress=None):
        p = progress if progress is not None else {}
        self.calls.append(("recreate", dep["name"], image, migrate))
        self._maybe("recreate:pull")
        p["stopped"] = True
        self._maybe("recreate:stopped")
        if migrate:
            p["migrated"] = True
        self._maybe("recreate:migrated")
        p["created"] = True
        return {"image": image, "ops_user": 1}

    def stop(self, dep):
        self.calls.append(("stop", dep["name"]))
        self._maybe("stop")

    def start(self, dep):
        self.calls.append(("start", dep["name"]))
        self._maybe("start")

    def put_files(self, dep):
        self.calls.append(("put_files", dep["name"]))

    def delete(self, dep):
        self.calls.append(("delete", dep["name"]))
        self._maybe("delete")

    def check_dags(self, dep, image, data):
        return {"airflow": "3.3.2", "dags": ["d"], "import_errors": {}}

    def image_version(self, dep, image):
        return "3.3.2"

    def sync_dags(self, dep, release, data):
        self.calls.append(("sync_dags", dep["name"], release))

    def current_release(self, dep):
        return None

    def refresh_files(self, dep, files):
        pass

    def state(self, dep):
        return {"runtime": "docker", "components": []}

    def logs(self, dep, component, tail=200):
        return ""


class Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self):
        pass

    def put(self, key, data):
        self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def delete_prefix(self, prefix):
        for k in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[k]


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


def dags() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"from airflow.sdk import DAG\n"
        info = tarfile.TarInfo("d.py")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from cairn.airflow_deploy.identity import ISSUER_NAME
    from cairn.dataaccess.trino_auth import TrinoTokens
    from cairn.dataaccess.trino_oauth import TrinoOAuth
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.identity.workload import WorkloadIdentity

    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"airflow": {"deployments": ["3.3.2", "3.3.9"]}}))
    monkeypatch.setenv("CAIRN_VERSION_PINS", str(pins))
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    load_models()                                         # every table (the pipeline user's grants are in several)
    metadata.create_all(engine)
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    scopes.create(ADMIN, {"name": "team"})
    scopes.put(ADMIN, "team", "pw", "s3cr3t")
    keys = Keys(b"q" * 32)
    tokens = TrinoTokens(engine, "k" * 40, "cairn", 3600)
    wi = WorkloadIdentity({ISSUER_NAME: keys.issuer(str(tmp_path))}, engine, tokens)
    clock = Clock(MONDAY_10)
    s = AirflowDeployments(engine, lambda a, p, u: True, None, keys, Runtime(), Store(), "http://cairn:8800",
                           "https://cairn.test", {}, b"", scopes, TrinoOAuth(engine, tokens, "", []), wi, "",
                           clock=clock)
    s.clock_ = clock
    return s


def make(svc, name, **spec):
    return svc.create(ADMIN, {"name": name, "environment": spec.pop("environment", "dev"), **spec})


# ---- 2. approval ------------------------------------------------------------------------------------------------
def test_an_environment_that_requires_approval_takes_changes_only_through_an_approved_promotion(svc):
    make(svc, "afstage", environment="stage", approval={"required": True})
    make(svc, "afdev", promotes_to="afstage")
    first = svc.deploy_dags(ADMIN, "afdev", dags())
    # Direct deploys of every kind are refused, before anything is touched.
    for attempt in (lambda: svc.deploy_dags(ADMIN, "afstage", dags()),
                    lambda: svc.deploy_image(ADMIN, "afstage", "registry.example.com/team/airflow:1"),
                    lambda: svc.deploy_git(ADMIN, "afstage")):
        with pytest.raises(DeploymentError, match=APPROVAL_ONLY) as e:
            attempt()
        assert e.value.status == 409
    assert svc.deploys(ADMIN, "afstage") == [] and svc._row("afstage").state == "ready"

    # Through a promotion: pending until someone else approves; then it lands, and is an approved release.
    p = svc.promote(RAJ, "afdev", {})
    assert p["state"] == "pending" and svc.deploys(ADMIN, "afstage") == []
    with pytest.raises(DeploymentError, match="someone other"):
        svc.decide(RAJ, p["id"], True)
    applied = svc.decide(ANA, p["id"], True)
    assert applied["state"] == "applied" and applied["decided_by"] == ANA
    promoted = applied["result_deploy"]
    assert svc._approved_release("afstage", promoted)

    # A second release, then rolling back: to the approved one, and to a rollback of it, yes.
    svc.deploy_dags(ADMIN, "afdev", dags())
    p2 = svc.decide(ANA, svc.promote(RAJ, "afdev", {})["id"], True)
    back = svc.rollback(RAJ, "afstage", promoted)
    assert back["source"] == f"rollback:{promoted}"
    svc.rollback(RAJ, "afstage", p2["result_deploy"])
    again = svc.rollback(RAJ, "afstage", back["id"])        # a rollback of a rollback of an approved release
    assert again["state"] == "live"

    # A deploy that did not come through an approved promotion is not rolled back to. (One recorded directly, as
    # before approval was required; and one promoted while the target needed no approval.)
    with svc.engine.begin() as c:
        c.execute(insert(deploys_table).values(
            id="direct0000000001", deployment="afstage", seq=99, kind="dags", state="superseded",
            bundle_key=svc._deploy_row("afstage", promoted).bundle_key, created_by=ADMIN, created_ms=1))
    with pytest.raises(DeploymentError, match=APPROVAL_ONLY) as e:
        svc.rollback(RAJ, "afstage", "direct0000000001")
    assert e.value.status == 409
    make(svc, "afqa", environment="stage")                # no approval: promotions apply at once, decided by no one
    auto = svc.promote(RAJ, "afdev", {"to": "afqa"})
    assert auto["state"] == "applied" and auto["decided_by"] is None
    svc.update(ADMIN, "afqa", {"approval": {"required": True}})
    with pytest.raises(DeploymentError, match=APPROVAL_ONLY):
        svc.rollback(RAJ, "afqa", auto["result_deploy"])
    assert first["state"] == "live"


def test_the_api_refuses_a_direct_deploy_to_an_approval_environment_with_409(svc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.airflow_deploy.deployments import build_airflow_router

    class P:
        urn = ADMIN

    make(svc, "afprod", environment="prod")              # prod requires approval by default
    app = FastAPI()
    app.include_router(build_airflow_router(svc, lambda: P()))
    c = TestClient(app)
    r = c.post("/v1/airflow/deployments/afprod/deploys", content=dags(), headers={"content-type": "application/gzip"})
    assert r.status_code == 409 and APPROVAL_ONLY in r.json()["detail"]
    r = c.post("/v1/airflow/deployments/afprod/deploys", json={"image": "registry.example.com/team/airflow:1"})
    assert r.status_code == 409 and APPROVAL_ONLY in r.json()["detail"]


# ---- 3. a failed wake -------------------------------------------------------------------------------------------
def test_a_failed_wake_stops_what_it_started_and_the_schedule_tries_again_with_backoff(svc):
    rt, clock = svc.runtime, svc.clock_
    make(svc, "afdev", hibernation=HOURS)
    svc.hibernate("", "afdev")                            # as the scheduler does: no hold
    rt.fail["wait_ready"] = "the scheduler never became healthy"
    rt.calls.clear()
    with pytest.raises(DeploymentError, match="never became healthy"):
        svc.wake(ADMIN, "afdev")
    assert [c[0] for c in rt.calls] == ["put_files", "start", "wait_ready", "stop"]   # what it started, stopped
    row = svc._row("afdev")
    assert row.state == "hibernating" and "never became healthy" in row.error and row.lease_ms is None
    assert row.wake_failures == 1 and row.wake_retry_ms == MONDAY_10 + WAKE_BACKOFF_MS

    # The schedule (awake hours) does not try again before the backoff, then does, waiting twice as long next.
    rt.calls.clear()
    clock.ms += WAKE_BACKOFF_MS - 1
    assert svc.run_due() == 0 and not rt.calls
    clock.ms += 1
    assert svc.run_due() == 0                             # tried, failed again (said in the log, not raised)
    assert ("start", "afdev") in rt.calls and ("stop", "afdev") in rt.calls
    row = svc._row("afdev")
    assert row.wake_failures == 2 and row.wake_retry_ms == clock.ms + 2 * WAKE_BACKOFF_MS

    # Fixed: the next try wakes it, and the failures are forgotten.
    del rt.fail["wait_ready"]
    clock.ms = row.wake_retry_ms
    assert svc.run_due() == 1
    row = svc._row("afdev")
    assert row.state == "ready" and row.error is None and row.wake_failures is None and row.wake_retry_ms is None


def test_a_wake_refused_before_anything_started_stops_nothing(svc):
    rt = svc.runtime
    make(svc, "afdev", hibernation=HOURS, secrets=[])
    svc.hibernate(ADMIN, "afdev")
    svc._set("afdev", spec=json.dumps({**json.loads(svc._row("afdev").spec),
                                      "secrets": [{"env": "PW", "scope": "team", "key": "pw"}]}))
    rt.calls.clear()
    with pytest.raises(DeploymentError, match="cannot be read"):
        svc.wake(ADMIN, "afdev")
    assert rt.calls == [] and svc._row("afdev").state == "hibernating"


# ---- 4. failed restarts, upgrades and image deploys ------------------------------------------------------------------
def test_a_failed_restart_is_failed_with_the_settings_it_was_made_with(svc):
    rt = svc.runtime
    make(svc, "afdev", env={"A": "1"})
    rt.fail["wait_ready"] = "api server crash-looping"
    with pytest.raises(DeploymentError, match="crash-looping"):
        svc.update(ADMIN, "afdev", {"env": {"A": "2"}})
    row = svc._row("afdev")
    assert row.state == "failed" and "crash-looping" in row.error and row.lease_ms is None
    assert json.loads(row.spec)["env"] == {"A": "2"}       # the components run these settings now
    # A failed deployment can be fixed by another change.
    del rt.fail["wait_ready"]
    svc.update(ADMIN, "afdev", {"env": {"A": "3"}})
    assert svc._row("afdev").state == "ready" and svc._row("afdev").error is None


def test_a_failed_upgrade_records_the_migrated_database(svc):
    rt = svc.runtime
    make(svc, "afdev")
    rt.fail["recreate:migrated"] = "the API server image would not start"
    with pytest.raises(DeploymentError, match="migrated to Airflow 3.3.9"):
        svc.update(ADMIN, "afdev", {"airflow_version": "3.3.9"})
    row = svc._row("afdev")
    assert row.state == "failed" and json.loads(row.spec)["airflow_version"] == "3.3.9"
    assert row.image == "cairn/airflow:0.2.0" and "migrated to Airflow 3.3.9" in row.error


def test_a_change_refused_before_anything_was_touched_leaves_the_deployment_as_it_was(svc):
    rt = svc.runtime
    make(svc, "afdev", env={"A": "1"})
    rt.fail["recreate:pull"] = "image not found"
    with pytest.raises(DeploymentError, match="image not found"):
        svc.update(ADMIN, "afdev", {"airflow_version": "3.3.9"})
    row = svc._row("afdev")
    assert row.state == "ready" and row.error is None and json.loads(row.spec)["airflow_version"] == "3.3.2"
    assert row.lease_ms is None


def test_a_failed_image_deploy_is_a_failed_deploy_and_a_failed_deployment(svc):
    rt = svc.runtime
    make(svc, "afdev")
    team = "registry.example.com/team/airflow:2"
    rt.fail["wait_ready"] = "scheduler exited: ModuleNotFoundError"
    with pytest.raises(DeploymentError, match="ModuleNotFoundError"):
        svc.deploy_image(ADMIN, "afdev", team)
    [d] = svc.deploys(ADMIN, "afdev")
    assert d["kind"] == "image" and d["state"] == "failed" and "ModuleNotFoundError" in d["error"]
    row = svc._row("afdev")
    assert row.state == "failed" and row.image == team and row.custom_image == team and row.lease_ms is None
    del rt.fail["wait_ready"]
    good = svc.deploy_image(ADMIN, "afdev", "registry.example.com/team/airflow:3")
    assert good["state"] == "live" and svc._row("afdev").state == "ready"


# ---- 5. delete during a change -------------------------------------------------------------------------------------
def test_delete_waits_for_a_change_another_request_holds(svc):
    clock = svc.clock_
    make(svc, "afdev")
    svc._claim("afdev", "updating", ("ready",))           # another request is changing it
    with pytest.raises(DeploymentError, match="a change is in progress") as e:
        svc.delete(ADMIN, "afdev")
    assert e.value.status == 409 and svc._row("afdev").state == "updating"
    assert ("delete", "afdev") not in svc.runtime.calls
    clock.ms += LEASE_MS + 1                              # its holder died: the lease ran out
    svc.delete(ADMIN, "afdev")
    with pytest.raises(DeploymentError, match="no such deployment"):
        svc._row("afdev")


def test_a_kubernetes_deployment_still_coming_up_has_no_holder_and_may_be_deleted(svc):
    make(svc, "afdev")
    svc._set("afdev", state="creating", lease_ms=None)    # as a Kubernetes create leaves it, its pods coming up
    svc.delete(ADMIN, "afdev")


def test_the_docker_runtime_reports_a_volume_still_in_use_instead_of_ignoring_it(monkeypatch):
    from cairn.airflow_deploy.docker_runtime import DockerRuntime

    rt = DockerRuntime(socket="/nonexistent.sock")
    calls = []
    busy = {"cairn-af-x-logs": 2}                         # in use twice, then free

    def call(method, path, ok=(200,), **kw):
        calls.append((method, path))
        if method == "GET":
            return [{"Id": "abc"}] if path == "/containers/json" else None
        name = path.rsplit("/", 1)[1]
        if busy.get(name):
            busy[name] -= 1
            raise RuntimeError_(f"docker: {method} {path}: 409 volume is in use", 409)
        return b""
    monkeypatch.setattr(rt, "_call", call)
    monkeypatch.setattr(rt, "_remove", lambda c: calls.append(("rm", c)))
    rt.delete({"name": "x"}, pause=0)
    assert ("rm", "abc") in calls and ("rm", "cairn-af-x-init-dags") in calls   # labelled and one-shot containers
    assert calls.count(("DELETE", "/volumes/cairn-af-x-logs")) == 3

    busy["cairn-af-x-net"] = 99                           # still in use: reported, the deployment not deleted
    with pytest.raises(RuntimeError_, match="network cairn-af-x-net") as e:
        rt.delete({"name": "x"}, attempts=3, pause=0)
    assert e.value.status == 409


def test_a_runtime_that_cannot_delete_leaves_the_deployment_failed_with_the_reason(svc):
    make(svc, "afdev")
    svc.runtime.fail["delete"] = "volume cairn-af-afdev-db still in use"
    with pytest.raises(DeploymentError, match="still in use"):
        svc.delete(ADMIN, "afdev")
    row = svc._row("afdev")
    assert row.state == "failed" and "still in use" in row.error and row.lease_ms is None


# ---- 6. ports ----------------------------------------------------------------------------------------------------
def test_deployments_made_at_once_never_share_a_port(svc):
    rt = svc.runtime
    make(svc, "afone")
    make(svc, "aftwo")                                    # no container binds afone's port: its record does
    assert svc._row("afone").port == 18080 and svc._row("aftwo").port == 18081
    # Two requests that picked the same port at once: the unique index makes the later one pick again.
    first = [True]
    real = svc._ports_in_use

    def racing():                                         # the later request read the records before afone's
        if first[0]:
            first[0] = False
            return set()
        return real()
    svc._ports_in_use = racing
    make(svc, "afthree")
    assert svc._row("afthree").port == 18082
    assert [c for c in rt.calls if c[0] == "allocate_port"][-2:] == [("allocate_port", 18080), ("allocate_port", 18082)]


def test_the_docker_runtime_never_hands_out_an_excluded_port(monkeypatch):
    from cairn.airflow_deploy.docker_runtime import DockerRuntime

    rt = DockerRuntime(socket="/nonexistent.sock", ports=(20000, 20002))
    monkeypatch.setattr(rt, "_taken_ports", lambda: {20001})
    assert rt.allocate_port() == 20000
    assert rt.allocate_port({20000}) == 20002
    with pytest.raises(RuntimeError_, match="no free port"):
        rt.allocate_port({20000, 20002})


# ---- 7. the pipeline user's grants ----------------------------------------------------------------------------------
def test_the_pipeline_users_grants_go_with_its_deployment(svc):
    from cairn.dataaccess.volumes import volume_grants
    from cairn.governance.personas import grants_table
    from cairn.identity.secret_scopes import secret_acls

    user = "urn:li:corpuser:svc-af-afdev"
    svc.secret_scopes.set_acl(ADMIN, "team", user, "READ")
    make(svc, "afdev", secrets=[{"env": "PW", "scope": "team", "key": "pw"}])
    with svc.engine.begin() as c:
        c.execute(insert(grants_table).values(id="g1", persona_id="analysts", actor=user, source="test",
                                              expires_at_ms=None, revoked=False, created_at_ms=1))
        c.execute(insert(volume_grants).values(volume_id="v1", principal=user, access="read", granted_by=ADMIN,
                                               created_ms=1))
    svc.delete(ADMIN, "afdev")
    with svc.engine.connect() as c:
        assert c.execute(select(secret_acls).where(secret_acls.c.principal == user)).all() == []
        assert c.execute(select(grants_table.c.revoked).where(grants_table.c.id == "g1")).scalar() is True
        assert c.execute(select(volume_grants).where(volume_grants.c.principal == user)).all() == []
    # A deployment made later under the same name starts with nothing: its secret cannot be read.
    with pytest.raises(DeploymentError, match="cannot be read by urn:li:corpuser:svc-af-afdev"):
        make(svc, "afdev", secrets=[{"env": "PW", "scope": "team", "key": "pw"}])


def test_a_name_in_use_is_refused_before_its_secrets_are_read(svc):
    make(svc, "afdev")
    reads = []
    real = svc.secret_scopes.read
    svc.secret_scopes.read = lambda *a: reads.append(a) or real(*a)
    with pytest.raises(DeploymentError, match="afdev exists") as e:
        make(svc, "afdev", secrets=[{"env": "PW", "scope": "team", "key": "pw"}])
    assert e.value.status == 409 and reads == []


# ---- 8. a manual hibernation holds -----------------------------------------------------------------------------------
def test_a_manual_hibernation_holds_until_the_schedules_next_wake(svc):
    clock = svc.clock_
    make(svc, "afdev", hibernation=HOURS)
    out = svc.hibernate(ADMIN, "afdev")                   # Monday 10:00, awake hours
    tuesday_8 = int(dt.datetime(2026, 11, 17, 8, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert out["state"] == "hibernating" and out["held_until_ms"] == tuesday_8
    for h in (1, 8, 12):                                  # Monday 11:00 (awake hours), 18:00, 22:00
        clock.ms = MONDAY_10 + h * 3600_000
        svc.run_due()
        assert svc._row("afdev").state == "hibernating", h
    clock.ms = tuesday_8 + 60_000
    assert svc.run_due() == 1 and svc._row("afdev").state == "ready"
    # A manual wake ends a hold at once; the scheduler's own hibernation holds nothing.
    svc.hibernate(ADMIN, "afdev")
    svc.wake(ADMIN, "afdev")
    assert svc._row("afdev").hold_until_ms is None
    clock.ms = int(dt.datetime(2026, 11, 17, 20, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    svc._set("afdev", awake_until_ms=None)
    svc.run_due()                                         # outside hours: the scheduler hibernates it
    assert svc._row("afdev").state == "hibernating" and svc._row("afdev").hold_until_ms is None


def test_next_wake_across_a_weekend_and_time_zones():
    from cairn.airflow_deploy.spec import check_hibernation, next_wake

    friday_20 = dt.datetime(2026, 11, 20, 20, 0, tzinfo=dt.timezone.utc)
    hours = check_hibernation(HOURS, "dev")
    assert next_wake(hours, friday_20) == dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.timezone.utc)
    berlin = check_hibernation({**HOURS, "timezone": "Europe/Berlin"}, "dev")
    assert next_wake(berlin, friday_20) == dt.datetime(2026, 11, 23, 7, 0, tzinfo=dt.timezone.utc)
    always = check_hibernation({"awake": [{"days": "mon-sun", "from": "00:00", "to": "23:59"}]}, "dev")
    assert next_wake(always, friday_20) == dt.datetime(2026, 11, 21, 0, 0, tzinfo=dt.timezone.utc)
    assert next_wake(None, friday_20) is None


# ---- minor: bundles ---------------------------------------------------------------------------------------------------
def test_a_bundles_folders_count_towards_its_entries():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for i in range(bundles.MAX_FILES):
            d = tarfile.TarInfo(f"d{i}")
            d.type = tarfile.DIRTYPE
            t.addfile(d)
        f = tarfile.TarInfo("dag.py")
        f.size = 1
        t.addfile(f, io.BytesIO(b"\n"))
    with pytest.raises(bundles.BundleError, match="files and folders"):
        bundles.inspect(buf.getvalue())


def test_a_bundle_fits_the_apis_body_limit():
    default = 32 << 20                                    # cairn.app: CAIRN_GATEWAY_MAX_BODY_BYTES
    assert bundles.MAX_UPLOAD <= int(os.environ.get("CAIRN_GATEWAY_MAX_BODY_BYTES", default))
