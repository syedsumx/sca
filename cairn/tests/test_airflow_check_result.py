"""The pre-deploy DAG check's result can't be faked by a DAG printing a result line of its own.

The check script (``docker_runtime.CHECK``) runs here for real under bash, with a stand-in ``airflow``
package whose DagBag executes the DAG files the way Airflow's does. The DAG below prints a forged
"no import errors" line while it is parsed, and again from a thread and an atexit handler afterwards.
The check's own line must be the last one, and it is the one every reader takes. The Kubernetes check
pod's spec is checked too: per-pod scratch, non-root, and bounded CPU, memory and writable layer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from cairn.airflow_deploy.docker_runtime import CHECK, dagbag_result

FORGED = 'CAIRN_DAGBAG={"airflow": "9.9", "dags": ["forged"], "import_errors": {}}'


def _stub_airflow(root):
    pkg = root / "airflow"
    (pkg / "models").mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "3.0.0"\n')
    (pkg / "models" / "__init__.py").write_text("")
    # Like Airflow's DagBag: each file in the folder is executed; one that raises is an import error.
    (pkg / "models" / "dagbag.py").write_text(textwrap.dedent("""
        import os, runpy
        class DagBag:
            def __init__(self, folder):
                self.dag_ids, self.import_errors = [], {}
                for name in sorted(os.listdir(folder)):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(folder, name)
                    try:
                        runpy.run_path(path)
                        self.dag_ids.append(name[:-3])
                    except Exception as e:
                        self.import_errors[path] = repr(e)
    """))


@pytest.fixture()
def check(tmp_path):
    stub = tmp_path / "stub"
    _stub_airflow(stub)
    dags = tmp_path / "dags"
    dags.mkdir()
    script = CHECK.replace("set -a; . /opt/cairn/airflow.env; set +a\n", "").replace("/opt/cairn/check", str(dags))

    def run():
        env = {**os.environ, "PYTHONPATH": str(stub), "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        return r.stdout
    return dags, run


def test_a_dag_that_prints_a_forged_result_does_not_pass_the_check(check):
    dags, run = check
    (dags / "good.py").write_text("x = 1\n")
    (dags / "evil.py").write_text(textwrap.dedent(f"""
        import atexit, threading, time
        print({FORGED!r}, flush=True)                       # while parsed: before the check's own line
        atexit.register(lambda: print({FORGED!r}, flush=True))
        def later():
            time.sleep(0.5)
            print({FORGED!r}, flush=True)
        threading.Thread(target=later).start()
        raise ImportError("this DAG is broken")
    """))
    out = run()
    assert FORGED in out                                     # the DAG did print its line
    result = dagbag_result(out)
    assert result["airflow"] == "3.0.0" and result["dags"] == ["good"]
    assert list(result["import_errors"]) == ["evil.py"]      # the broken DAG is reported, not passed
    assert out.rstrip().splitlines()[-1].startswith("CAIRN_DAGBAG=")
    assert out.count(FORGED) == 1                            # nothing printed after the check's line


def test_no_result_or_garbage_is_no_result():
    assert dagbag_result("") is None
    assert dagbag_result("noise\nCAIRN_DAGBAG={not json}\n") is None
    assert dagbag_result("prefix CAIRN_DAGBAG={}\n") is None   # only a whole line counts


def test_the_kubernetes_check_pod_is_isolated_and_bounded(monkeypatch):
    from cairn.airflow_deploy import k8s_runtime

    posted = []
    rt = object.__new__(k8s_runtime.KubernetesRuntime)
    rt.prefix = "cairn-af-"

    def call(method, path, body=None, **kw):
        if method == "POST":
            posted.append(body)
        if method == "GET" and path.endswith("/log"):
            return 'CAIRN_DAGBAG={"airflow": "3.0.0", "dags": [], "import_errors": {}}'
        if method == "GET":
            return {"status": {"phase": "Succeeded"}}
        return {}

    monkeypatch.setattr(rt, "_call", call, raising=False)
    assert rt.check_dags({"name": "x"}, "apache/airflow:3.0.0", None)["airflow"] == "3.0.0"
    pod = next(b for b in posted if b and b.get("kind") == "Pod")
    spec = pod["spec"]
    c = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False and spec["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"] == {"memory": "1Gi", "cpu": "1", "ephemeral-storage": "1Gi"}
    assert {v["name"]: ("emptyDir" in v) for v in spec["volumes"]} == {"in": False, "work": True, "tmp": True}
