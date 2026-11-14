"""A flow as an Airflow DAG file (golden files), the runner option, deploying to a DAG folder (a directory and a real
git repository), and converting DAG tasks back to flow steps. Airflow itself runs in tests/test_flows_airflow_e2e.py.

Regenerate the golden files with CAIRN_UPDATE_GOLDEN=1 after a deliberate change to the generator."""

from __future__ import annotations

import ast
import os
import subprocess

import pytest

from cairn.flows import model
from cairn.flows.airflow_dag import native_reason, to_dag
from cairn.flows.airflow_interop import AirflowDags, DagFolder, _calls, _step
from cairn.flows.model import FlowError

HERE = os.path.join(os.path.dirname(__file__), "data", "airflow_golden")
ANA = "urn:li:corpuser:ana"


def nightly() -> dict:
    return model.normalize(model.from_text(open(os.path.join(HERE, "nightly.yaml")).read()))


@pytest.mark.parametrize("mode,kwargs", [("native", {"operators": "native"}), ("cairn", {"operators": "cairn"}),
                                         ("runner", {"runner": True})])
def test_golden_files(mode, kwargs):
    text = to_dag(nightly(), owner=ANA, version=3, **kwargs)
    path = os.path.join(HERE, f"nightly-{mode}.py")
    if os.environ.get("CAIRN_UPDATE_GOLDEN") == "1":
        with open(path, "w") as fh:
            fh.write(text)
    assert text == open(path).read(), f"the generated DAG changed: CAIRN_UPDATE_GOLDEN=1 to accept ({path})"
    compile(text, path, "exec")


def _tasks(text: str) -> dict[str, tuple[str, dict]]:
    """task id -> (operator class, its literal keyword arguments), read from the generated file."""
    out = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            kws = {k.arg: k.value for k in node.keywords if k.arg}
            if "task_id" in kws:
                vals = {}
                for k, v in kws.items():
                    try:
                        vals[k] = ast.literal_eval(v)
                    except ValueError:
                        vals[k] = ast.unparse(v)
                out[vals["task_id"]] = (node.func.id, vals)
    return out


def test_what_maps_natively_and_what_is_delegated():
    text = to_dag(nightly(), owner=ANA, version=3)
    tasks = _tasks(text)
    assert {k: v[0] for k, v in tasks.items()} == {
        "refresh": "CairnFlowStepOperator", "enough": "CairnCheckOperator", "build": "CairnDbtOperator",
        "score": "CairnSparkApplicationOperator", "export": "TriggerDagRunOperator",
        "rows": "CairnFlowStepOperator",                         # its output is read by the branch
        "big": "CairnFlowBranchOperator", "signoff": "CairnFlowStepOperator", "note": "CairnFlowStepOperator",
        "each": "CairnFlowStepOperator", "done": "CairnFlowStepOperator"}
    # Retries, delays, back-off, time limits and trigger rules carry over.
    refresh = tasks["refresh"][1]
    assert refresh["retries"] == 2 and refresh["retry_delay"] == "timedelta(seconds=60)"
    assert refresh["retry_exponential_backoff"] is True and refresh["execution_timeout"] == "timedelta(seconds=21600)"
    assert tasks["done"][1]["trigger_rule"] == "all_done" and tasks["done"][1]["execution_timeout"] == "timedelta(seconds=120)"
    assert tasks["big"][1]["then"] == ["signoff"] and tasks["big"][1]["otherwise"] == ["note"]
    assert tasks["export"][1]["wait_for_completion"] is True and tasks["export"][1]["poke_interval"] == 60
    assert tasks["enough"][1]["op"] == ">=" and "{{ params.region }}" in tasks["enough"][1]["sql"]
    # The order: every edge of the flow, nothing more.
    edges = {(a, b) for s in nightly()["steps"] for a in s["after"] for b in [s["name"]]}
    got = set()
    for line in text.splitlines():
        if " >> " in line:
            a, b = line.strip().split(" >> ")
            got.add((a[len("steps['"):-2], b[len("steps['"):-2]))
    assert got == edges
    # Schedule in its time zone, the table trigger as a watched asset, both together; what does not map is said.
    assert "CronTriggerTimetable('0 2 * * *', timezone='Europe/Berlin')" in text and "AssetOrTimeSchedule" in text
    assert "CairnDatasetChangedTrigger(['raw.orders']" in text
    assert "Trigger after flow ingest-orders" in text and "SLA 90 minutes" in text and "concurrency skip" in text
    assert "'day': Param(None" in text and "'min_rows': Param(1, type='integer')" in text
    # Every delegated step says why, and the file holds no secret, only the connection's id.
    for name, (cls, _) in tasks.items():
        if cls == "CairnFlowStepOperator":
            assert f"# {name} (" in text and "delegated to Cairn" in text
    assert "token" not in text.lower().replace("projected token", "").replace("a cairn token of a pipeline user", "")


def test_cairn_operators_and_the_runner_delegate_every_step():
    for kwargs in ({"operators": "cairn"}, {"runner": True}):
        tasks = _tasks(to_dag(nightly(), owner=ANA, version=3, **kwargs))
        assert {v[0] for k, v in tasks.items() if k != "big"} == {"CairnFlowStepOperator"}
    runner = to_dag(nightly(), owner=ANA, version=3, runner=True)
    assert "dag_id='cairn__nightly-sales'" in runner and "schedule=None" in runner
    assert "'cairn-flow-version:3'" in runner and "Cairn starts them" in runner


def test_templates_decide_native_or_delegated():
    read: set = set()
    assert native_reason({"name": "a", "type": "sql", "sql": "SELECT {{ params.x }}", "connection": "c"}, read) is None
    assert "templates" in native_reason({"name": "a", "type": "sql", "sql": "SELECT {{ run.logical_date }}",
                                         "connection": "c"}, read)
    assert "saved query" in native_reason({"name": "a", "type": "sql", "query": "q1", "sql": None, "connection": None},
                                          read)
    assert "JVM" in native_reason({"name": "a", "type": "spark", "application": "x.jar", "class_name": "a.B"}, read)
    assert "other steps" in native_reason({"name": "a", "type": "dbt", "command": "build", "project_dir": "/d"}, {"a"})
    with pytest.raises(ValueError):
        to_dag(nightly(), owner=ANA, operators="mixed")


def test_runner_option():
    base = {"name": "r", "steps": [{"name": "a", "type": "notify", "message": "hi"}]}
    assert model.normalize(base)["runner"] == "cairn"
    assert model.normalize({**base, "runner": "airflow"})["runner"] == "airflow"
    assert "runner: airflow" in model.to_yaml(model.normalize({**base, "runner": "airflow"}))
    assert "runner" not in model.to_yaml(model.normalize(base))
    with pytest.raises(FlowError) as e:
        model.normalize({**base, "runner": "argo"})
    assert e.value.errors[0]["path"] == "runner"


def test_dag_folder_on_a_path(tmp_path):
    folder = DagFolder(f"path:{tmp_path / 'dags'}")
    out = folder.write("nightly", "print('x')\n", "m")
    assert out["file"] == "cairn__nightly.py" and (tmp_path / "dags" / "cairn__nightly.py").read_text() == "print('x')\n"
    assert oct((tmp_path / "dags" / "cairn__nightly.py").stat().st_mode)[-3:] == "644"
    folder.write("nightly", None, "m")
    assert not (tmp_path / "dags" / "cairn__nightly.py").exists()
    with pytest.raises(ValueError):
        DagFolder("s3://bucket/dags")


def test_dag_folder_in_a_git_folder(tmp_path, engine):
    """The DAG is committed onto a git folder's branch (what Airflow's git-sync reads), in a real repository."""
    from cairn.sqlworkspace.gitfolders import GitFolders

    remote = tmp_path / "dags.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README").write_text("dags\n")
    for args in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "--quiet", "-m", "init"],
                 ["push", "--quiet", "origin", "HEAD:main"]):
        subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)
    gf = GitFolders(engine, None, workdir=str(tmp_path / "work"), allowed_urls=["file://"])
    f = gf.create("urn:li:corpuser:ops", {"name": "dags", "url": f"file://{remote}", "branch": "main", "path": "cairn"})
    folder = DagFolder(f"git:{f['id']}", gf)
    out = folder.write("nightly", "# dag\n", "Cairn: deploy flow nightly version 1")
    assert out["commit"]
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"], capture_output=True, text=True)
    assert log.stdout.splitlines()[0] == "Cairn: deploy flow nightly version 1"
    show = subprocess.run(["git", "--git-dir", str(remote), "show", "main:cairn/cairn__nightly.py"], capture_output=True,
                          text=True)
    assert show.stdout == "# dag\n"
    folder.write("nightly", None, "Cairn: remove flow nightly")
    gone = subprocess.run(["git", "--git-dir", str(remote), "show", "main:cairn/cairn__nightly.py"], capture_output=True)
    assert gone.returncode != 0
    # A change request's commit goes to a branch of its own, never the folder's branch.
    pr = gf.commit_files("urn:li:corpuser:ops", f["id"], {"iac/change.yaml": "queries: []\n"}, "Change 1",
                         new_branch="iac/change-1")
    assert pr["branch"] == "cairn/iac-change-1"
    branches = subprocess.run(["git", "--git-dir", str(remote), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    assert set(branches) == {"main", "cairn/iac-change-1"}
    from cairn.sqlworkspace.workspace import WorkspaceError

    with pytest.raises(WorkspaceError):
        gf.commit_files("urn:li:corpuser:someone-else", f["id"], {"x.yaml": ""}, "m", new_branch="b")
    with pytest.raises(WorkspaceError):
        gf.commit_files("urn:li:corpuser:ops", f["id"], {"../escape.yaml": ""}, "m", new_branch="b")


SOURCE = '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
from cairn_airflow.operators import CairnTrinoOperator, CairnDbtOperator
TABLE = "x"
with DAG("orders_daily", start_date=datetime(2026, 1, 1), schedule="@daily"):
    start = EmptyOperator(task_id="start")
    load = CairnTrinoOperator(task_id="load", sql="INSERT INTO marts.daily SELECT * FROM raw.orders WHERE day = '{{ params.day }}'")
    dbt = CairnDbtOperator(task_id="dbt", project_dir="/dbt", command="build", select="tag:daily", retries=2)
    bad = CairnTrinoOperator(task_id="bad", sql="SELECT '{{ ds }}'")
    computed = CairnTrinoOperator(task_id="computed", sql=TABLE)
    start >> load >> dbt
'''


def test_task_arguments_and_their_mapping():
    calls = _calls(SOURCE)
    assert calls["dbt"]["select"] == "tag:daily" and calls["start"] == {"task_id": "start"}
    step = _step("CairnTrinoOperator", calls["load"], "lakehouse")
    assert step == {"type": "sql", "sql": calls["load"]["sql"], "connection": "lakehouse"}
    assert _step("CairnDbtOperator", calls["dbt"], "lakehouse") == {"type": "dbt", "command": "build",
                                                                    "project_dir": "/dbt", "select": "tag:daily"}
    from cairn.flows.airflow_interop import _Blocked

    for task, why in (("bad", "Airflow templates"), ("computed", "computed when the DAG file is read")):
        with pytest.raises(_Blocked, match=why):
            _step("CairnTrinoOperator", calls[task], "lakehouse")
    with pytest.raises(_Blocked, match="no flow step"):
        _step("PythonOperator", {"task_id": "p"}, "lakehouse")
    with pytest.raises(_Blocked, match="schema"):
        _step("CairnTrinoOperator", {"task_id": "s", "sql": "SELECT 1", "schema": "x"}, "lakehouse")
    with pytest.raises(_Blocked, match="Cairn flow"):
        _step("CairnFlowStepOperator", {"task_id": "s", "flow": "f", "step": "s"}, "lakehouse")


def test_who_sees_which_dag():
    groups = {"urn:li:corpuser:ana": ["urn:li:corpGroup:finance"], "urn:li:corpuser:bob": []}

    def groups_of(actor):
        if actor == "urn:li:corpuser:broken":
            raise RuntimeError("directory down")
        return groups[actor]
    dags = AirflowDags({}, groups_of, lambda actor: actor == "urn:li:corpuser:admin")
    fin = {"dag_id": "f", "owners": ["airflow"], "tags": [{"name": "team:finance"}]}
    own = {"dag_id": "o", "owners": ["bob"], "tags": []}
    grp = {"dag_id": "g", "owners": ["finance"], "tags": []}
    assert dags.visible("urn:li:corpuser:ana", fin) and dags.visible("urn:li:corpuser:ana", grp)
    assert not dags.visible("urn:li:corpuser:ana", own)
    assert dags.visible("urn:li:corpuser:bob", own) and not dags.visible("urn:li:corpuser:bob", fin)
    assert dags.visible("urn:li:corpuser:admin", own)
    # Groups that cannot be read count for nothing (fail closed).
    assert not dags.visible("urn:li:corpuser:broken", fin)
    # Airflow not configured: said so, not an empty list.
    with pytest.raises(FlowError) as e:
        dags.list("urn:li:corpuser:ana")
    assert e.value.status == 503
