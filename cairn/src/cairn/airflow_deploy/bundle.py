"""DAG bundles: a project's ``dags/`` folder as a gzipped tar, versioned in Cairn Store and synced into a deployment.

A bundle holds regular files and directories only (no links, devices or absolute paths, nothing outside the folder),
at most ``MAX_FILES`` files and ``MAX_BYTES`` unpacked. It is checked when it arrives and again when it is unpacked.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import time
from typing import Optional

MAX_FILES = 5000
MAX_BYTES = 200 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache"}


class BundleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def pack(folder: str) -> bytes:
    """The folder as a bundle (what ``cairn airflow deploy --dags-only`` sends)."""
    if not os.path.isdir(folder):
        raise BundleError(f"{folder}: no such folder")
    buf = io.BytesIO()
    n = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(root, d)))
            for f in sorted(files):
                full = os.path.join(root, f)
                if os.path.islink(full) or not os.path.isfile(full) or f.endswith((".pyc", ".pyo")):
                    continue
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                info = tarfile.TarInfo(rel)
                info.size, info.mode, info.mtime = os.path.getsize(full), 0o644, int(os.path.getmtime(full))
                with open(full, "rb") as fh:
                    t.addfile(info, fh)
                n += 1
                if n > MAX_FILES:
                    raise BundleError(f"more than {MAX_FILES} files in {folder}")
    data = buf.getvalue()
    if len(data) > MAX_UPLOAD:
        raise BundleError(f"the bundle is {len(data) // (1024 * 1024)} MB: at most {MAX_UPLOAD // (1024 * 1024)} MB")
    return data


def inspect(data: bytes) -> dict:
    """Checks a bundle and describes it: its files, size and digest. BundleError when it is not a safe one."""
    if not data:
        raise BundleError("the bundle is empty")
    if len(data) > MAX_UPLOAD:
        raise BundleError(f"a bundle is at most {MAX_UPLOAD // (1024 * 1024)} MB")
    files, total = [], 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            for m in t:
                name = m.name
                parts = name.replace("\\", "/").split("/")
                if name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                    raise BundleError(f"{name}: paths stay inside the DAG folder")
                if m.isdir():
                    continue
                if not m.isfile():
                    raise BundleError(f"{name}: only files and folders (no links or devices)")
                total += m.size
                files.append(name.lstrip("./"))
                if len(files) > MAX_FILES or total > MAX_BYTES:
                    raise BundleError(f"a bundle holds at most {MAX_FILES} files and {MAX_BYTES // (1024 * 1024)} MB")
    except tarfile.TarError as e:
        raise BundleError(f"not a tar archive: {e}")
    if not any(f.endswith(".py") for f in files):
        raise BundleError("the bundle has no Python file (a DAG folder's .py files)")
    return {"files": sorted(files), "bytes": total, "sha256": hashlib.sha256(data).hexdigest()}


def relocate(data: bytes, prefix: str, uid: int = 50000, gid: int = 0) -> bytes:
    """The bundle's files under ``prefix`` (a plain tar for Docker's archive API), owned by ``uid``."""
    inspect(data)
    out = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as src, tarfile.open(fileobj=out, mode="w") as dst:
        made: set[str] = set()

        def mkdir(path: str) -> None:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d and d not in made:
                    info = tarfile.TarInfo(d)
                    info.type, info.mode, info.uid, info.gid, info.mtime = tarfile.DIRTYPE, 0o755, uid, gid, now
                    dst.addfile(info)
                    made.add(d)
        mkdir(prefix.strip("/"))
        for m in src:
            if not m.isfile():
                continue
            rel = m.name.lstrip("./")
            path = f"{prefix.strip('/')}/{rel}"
            mkdir(os.path.dirname(path))
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.uid, info.gid, info.mtime = m.size, 0o644, uid, gid, m.mtime or now
            dst.addfile(info, src.extractfile(m))
    return out.getvalue()


def file_text(data: bytes, name: str, limit: int = 200_000) -> Optional[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
        for m in t:
            if m.isfile() and m.name.lstrip("./") == name:
                f = t.extractfile(m)
                return f.read(limit).decode("utf-8", "replace") if f else None
    return None
