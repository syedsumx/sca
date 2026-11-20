"""Another party's code started beside the API (installed app packages, process-runtime agent apps) cannot take the
API's secrets:
- the API marks itself not dumpable when it starts, so a child of the same OS user cannot read its
  /proc/<pid>/environ (where CAIRN_SERVICE_TOKEN is), proven with real processes as an unprivileged user;
- installs and process-runtime agent apps run beside the API only where the operator allows it
  (CAIRN_ALLOW_LOCAL_UNTRUSTED_CODE=true); installs go to the Kubernetes app runtime when it is configured."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import cairn
from cairn.common import procsec

SRC = os.path.dirname(os.path.dirname(cairn.__file__))
TOKEN = "dummy-service-token-for-the-isolation-test"
# A stand-in API: secrets in its environment, optionally hardened as create_api_app does, starting a child (same
# user, nothing of its environment) that reads the parent's /proc/<pid>/environ the way an app's code could.
STAND_IN = """
import subprocess, sys
from cairn.common.procsec import make_undumpable, dumpable
if sys.argv[1] == "harden":
    assert make_undumpable() and not dumpable()
child = "import os\\ntry:\\n print(open('/proc/%d/environ' % os.getppid(), 'rb').read().decode(errors='replace'))\\n" \\
        "except OSError as e:\\n print('refused', type(e).__name__)"
print(subprocess.run([sys.executable, "-I", "-c", child], env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                     text=True).stdout)
"""


def _stand_in(mode: str) -> str:
    kw = {}
    if os.geteuid() == 0:            # root reads any process's /proc; the API runs as an ordinary user
        kw = {"user": 65534, "group": 65534, "extra_groups": [], "cwd": "/"}
    r = subprocess.run([sys.executable, "-c", STAND_IN, mode], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "CAIRN_SERVICE_TOKEN": TOKEN}, **kw)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and prctl are Linux's")
def test_a_child_cannot_read_the_hardened_apis_environment():
    assert TOKEN in _stand_in("plain")          # the exposure: a same-user child reads the parent's environ
    out = _stand_in("harden")
    assert TOKEN not in out and "refused PermissionError" in out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux's")
def test_the_api_marks_itself_not_dumpable_at_start():
    # Checked in a fresh process: create_api_app is what makes it so.
    script = (
        "import tempfile\n"
        "from cairn.common.config import Settings\n"
        "from cairn.app import create_api_app\n"
        "from cairn.services import build_services\n"
        "from cairn.common.procsec import dumpable\n"
        "d = tempfile.mkdtemp()\n"
        "print(dumpable())\n"
        "s = build_services(Settings(database_url=f'sqlite:///{d}/c.db', service_token='svc'), with_ai=False,"
        " with_gateway=False)\n"
        "create_api_app(s)\n"
        "print(dumpable())\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHONPATH": SRC})
    assert r.returncode == 0, r.stderr[-3000:]
    assert r.stdout.split()[-2:] == ["True", "False"]


def _packages(monkeypatch, kube=None):
    from cairn.dataaccess.app_packages import AppPackages

    apps = SimpleNamespace(kube=kube)
    pk = AppPackages.__new__(AppPackages)
    pk.apps = apps
    prow = SimpleNamespace(git=json.dumps({"url": "file:///r", "branch": "main", "path": "app"}))
    vrow = SimpleNamespace(manifest=json.dumps({"title": "Churn", "command": ["python", "app.py", "{port}"],
                                                "idle_minutes": 30}), commit="abc")
    return lambda: pk._app_body(prow, vrow, "bob-churn")


def test_installs_need_kubernetes_or_the_operators_consent(monkeypatch):
    from cairn.dataaccess.app_packages import PackageError

    monkeypatch.delenv(procsec.LOCAL_UNTRUSTED, raising=False)
    monkeypatch.delenv("CAIRN_APP_PACKAGES_IMAGE", raising=False)
    with pytest.raises(PackageError) as e:
        _packages(monkeypatch)()
    assert e.value.status == 503 and procsec.LOCAL_UNTRUSTED in str(e.value)
    monkeypatch.setenv(procsec.LOCAL_UNTRUSTED, "true")
    assert _packages(monkeypatch)()["runtime"] == "local"
    # With the Kubernetes app runtime, installs run there (a pod of their own, another user), in the operator's image.
    with pytest.raises(PackageError) as e:
        _packages(monkeypatch, kube=object())()
    assert "CAIRN_APP_PACKAGES_IMAGE" in str(e.value)
    monkeypatch.setenv("CAIRN_APP_PACKAGES_IMAGE", "registry.example/apps:1")
    body = _packages(monkeypatch, kube=object())()
    assert (body["runtime"], body["image"]) == ("kubernetes", "registry.example/apps:1")


def test_an_existing_local_install_does_not_start_without_consent(monkeypatch, tmp_path):
    import asyncio

    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.dataaccess.dataapps import AppError, DataApps

    monkeypatch.delenv(procsec.LOCAL_UNTRUSTED, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path}/a.db")
    metadata.create_all(engine)
    apps = DataApps(engine, "s" * 32, workdir=str(tmp_path / "apps"))
    marker = tmp_path / "started"
    apps.create("urn:li:corpuser:admin", {"name": "inst", "command": [sys.executable, "-c",
                                                                       f"open({str(marker)!r}, 'w')"]})
    apps.isolated = lambda name: name == "inst"
    with pytest.raises(AppError) as e:
        asyncio.run(apps.upstream("inst"))
    assert e.value.status == 503 and not marker.exists()


def test_process_agent_apps_need_the_operators_consent(monkeypatch, tmp_path):
    from cairn.ai.agent_apps import AgentAppError, ProcessRuntime

    monkeypatch.delenv(procsec.LOCAL_UNTRUSTED, raising=False)
    rt = ProcessRuntime(tmp_path, "http://127.0.0.1:1")
    with pytest.raises(AgentAppError) as e:
        rt.start("triage", 1, b"")
    assert e.value.status == 503 and procsec.LOCAL_UNTRUSTED in str(e.value)
    assert not (tmp_path / "triage").exists()
    monkeypatch.setenv(procsec.LOCAL_UNTRUSTED, "true")
    ProcessRuntime(tmp_path, "http://127.0.0.1:1").check()
