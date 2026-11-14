"""The stand-in source connector's image for the connector tests: built with Docker from
``tests/connectors/standin-source``, pushed to a registry (``registry:2``) the test starts, and named by the digest
the registry gave it, so Cairn pulls it by digest as it would any connector image. Not a vendor's connector."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

CONTEXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectors", "standin-source")


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


def sh(*cmd: str, timeout: int = 300) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, f"{' '.join(cmd)}\n{p.stdout}\n{p.stderr}"
    return p.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def standin_image():
    """``127.0.0.1:<port>/cairn/standin-source@sha256:<digest>``: in the registry only (the local copy is removed)."""
    port, registry = free_port(), f"cairn-test-registry-{uuid.uuid4().hex[:8]}"
    sh("docker", "run", "-d", "--name", registry, "-p", f"127.0.0.1:{port}:5000", "registry:2")
    repo = f"127.0.0.1:{port}/cairn/standin-source"
    ref = None
    try:
        for _ in range(60):
            if subprocess.run(["curl", "-s", "-o", "/dev/null", "--noproxy", "*", f"http://127.0.0.1:{port}/v2/"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        sh("docker", "build", "-q", "-t", f"{repo}:test", CONTEXT)
        sh("docker", "push", "-q", f"{repo}:test")
        digests = json.loads(sh("docker", "image", "inspect", "-f", "{{json .RepoDigests}}", f"{repo}:test"))
        ref = next(d for d in digests if d.startswith(repo + "@sha256:"))
        sh("docker", "rmi", f"{repo}:test")              # Cairn pulls it, by digest
        yield ref
    finally:
        if ref:
            subprocess.run(["docker", "rmi", "-f", ref], capture_output=True)
        subprocess.run(["docker", "rm", "-f", "-v", registry], capture_output=True)
