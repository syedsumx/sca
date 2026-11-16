"""An agent project's bundle (the gzipped tar ``cairn agents deploy`` sends): unpacked safely, its requirements checked.

Shared by Cairn (the process runtime) and the agent runtime itself (``--bundle``, on Kubernetes), so it needs nothing
but the standard library."""

from __future__ import annotations

import io
import os
import re
import tarfile
from pathlib import Path


class BundleError(Exception):
    pass


# What a bundle may unpack to. The deploy caps its compressed size, but gzip turns a few megabytes into gigabytes,
# written to the API host's disk (process runtime) or the pod's /tmp (Kubernetes).
MAX_UNPACKED = int(os.environ.get("CAIRN_AGENT_APPS_MAX_UNPACKED", str(100 * 1024 * 1024)))
MAX_ENTRIES = int(os.environ.get("CAIRN_AGENT_APPS_MAX_ENTRIES", "10000"))


def unpack(data: bytes, into: Path) -> None:
    """Regular files and folders inside ``into`` only; the project must have an ``agent.py``."""
    into.mkdir(parents=True, exist_ok=True)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise BundleError(f"the bundle is not a gzipped tar: {e}")
    root = str(into.resolve())
    with tar:
        total, count = 0, 0
        for m in tar:                                  # headers first: nothing is written until every one is checked
            target = (into / m.name).resolve()
            if not str(target).startswith(root + os.sep) or not (m.isfile() or m.isdir()):
                raise BundleError(f"the bundle has an entry outside the project or not a file: {m.name}")
            count, total = count + 1, total + max(0, m.size)
            if count > MAX_ENTRIES:
                raise BundleError(f"the bundle has more than {MAX_ENTRIES} entries")
            if total > MAX_UNPACKED:
                raise BundleError(f"the bundle unpacks to more than {MAX_UNPACKED} bytes: leave data files out of "
                                  "the project")
        tar.extractall(into, filter="data")
    if not (into / "agent.py").is_file():
        raise BundleError("the bundle has no agent.py")


def missing_requirements(project: Path) -> list[str]:
    """The project's requirements (``requirements.txt``) not installed in this Python environment."""
    from importlib.metadata import PackageNotFoundError, distribution

    req = project / "requirements.txt"
    out = []
    for line in (req.read_text().splitlines() if req.is_file() else []):
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].strip()
        try:
            distribution(name)
        except PackageNotFoundError:
            out.append(name)
    return out
