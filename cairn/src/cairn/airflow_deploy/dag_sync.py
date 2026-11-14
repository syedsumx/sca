"""Keeps a managed Airflow deployment's DAG folder at the release Cairn says is live (the Kubernetes runtime).

Runs beside the scheduler, DAG processor and triggerer (and once, ``--once``, before each task pod starts), as the
deployment's pipeline user: its pod's projected service-account token is exchanged at Cairn
(``/v1/auth/workload-token``), and the live bundle is fetched from ``/v1/airflow/deployments/<name>/dags/current``.
A new release is unpacked beside the others (only files and folders, nothing outside the folder) and the ``current``
link swapped in one rename, the way git-sync does: Airflow keeps running. Standard library only.

Environment: ``CAIRN_URL``, ``CAIRN_AIRFLOW_DEPLOYMENT``, ``CAIRN_WORKLOAD_TOKEN_FILE``, ``CAIRN_CA_FILE`` (optional),
``CAIRN_DAGS_DIR`` (``/opt/airflow/dags``), ``CAIRN_DAG_SYNC_SECONDS`` (15).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

KEEP = 5


def _ctx():
    ca = os.environ.get("CAIRN_CA_FILE", "")
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def _open(req, timeout=60):
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx() if url.startswith("https://") else None)


def exchange(cairn: str, token_file: str) -> str:
    with open(token_file) as fh:
        subject = fh.read().strip()
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                                   "subject_token": subject,
                                   "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}).encode()
    with _open(urllib.request.Request(f"{cairn}/v1/auth/workload-token", data=body, method="POST")) as r:
        return json.loads(r.read())["access_token"]


def fetch(cairn: str, name: str, token: str):
    req = urllib.request.Request(f"{cairn}/v1/airflow/deployments/{urllib.parse.quote(name)}/dags/current",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with _open(req, 300) as r:
            return r.headers.get("X-Cairn-Release", ""), r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, b""
        raise


def unpack(dags: str, release: str, data: bytes) -> None:
    if not release or not all(c.isalnum() or c == "-" for c in release):
        raise ValueError(f"a bad release id {release!r}")
    releases = os.path.join(dags, "releases")
    os.makedirs(releases, exist_ok=True)
    target, tmp = os.path.join(releases, release), os.path.join(releases, f".{release}.tmp")
    if not os.path.isdir(target):
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            for m in t:
                if not (m.isfile() or m.isdir()):
                    raise ValueError(f"{m.name}: only files and folders")
            t.extractall(tmp, filter="data")
        os.rename(tmp, target)
    link = os.path.join(dags, ".cairn-next")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.path.join("releases", release), link)
    os.replace(link, os.path.join(dags, "current"))
    keep = sorted((d for d in os.listdir(releases) if not d.startswith(".") and d != release),
                  key=lambda d: os.path.getmtime(os.path.join(releases, d)), reverse=True)[KEEP:]
    for d in keep:
        shutil.rmtree(os.path.join(releases, d), ignore_errors=True)


def current(dags: str) -> str:
    try:
        return os.readlink(os.path.join(dags, "current")).rsplit("/", 1)[-1]
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    cairn = os.environ["CAIRN_URL"].rstrip("/")
    name = os.environ["CAIRN_AIRFLOW_DEPLOYMENT"]
    token_file = os.environ["CAIRN_WORKLOAD_TOKEN_FILE"]
    dags = os.environ.get("CAIRN_DAGS_DIR", "/opt/airflow/dags")
    period = int(os.environ.get("CAIRN_DAG_SYNC_SECONDS", "15"))
    once = "--once" in argv
    if not os.path.lexists(os.path.join(dags, "current")):
        os.makedirs(os.path.join(dags, "releases", "empty"), exist_ok=True)
        os.symlink(os.path.join("releases", "empty"), os.path.join(dags, "current"))
    failures = 0
    while True:
        try:
            release, data = fetch(cairn, name, exchange(cairn, token_file))
            if release and release != current(dags):
                unpack(dags, release, data)
                print(f"cairn dag sync: release {release} is live", flush=True)
            failures = 0
            if once:
                return 0
        except Exception as e:  # noqa: BLE001 - said, then tried again
            failures += 1
            print(f"cairn dag sync: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            if once and failures >= 20:
                return 1
        time.sleep(period if not once else 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
