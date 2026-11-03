"""The newest release of each component Cairn pins (deploy/versions/pins.json), and which are newer than
the pin. Sources: Docker Hub tags, PyPI and Maven Central; pre-releases are skipped.

    python deploy/versions/latest.py            # a table
    python deploy/versions/latest.py --matrix   # {"include": [{"component", "pinned", "latest"}]} for GitHub Actions

Only the standard library, so it runs before anything is installed."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

PINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pins.json")
PRE = re.compile(r"(a|b|rc|dev|alpha|beta|preview|snapshot)", re.I)


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cairn-upgrade-matrix"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def docker_tags(repo: str, pattern: str, get: Callable[[str], bytes] = _get, pages: int = 5) -> list[str]:
    rx, out = re.compile(pattern), []
    url: Optional[str] = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated"
    for _ in range(pages):
        if not url:
            break
        data = json.loads(get(url))
        for t in data.get("results") or []:
            m = rx.match(t.get("name", ""))
            if m:
                out.append(m.group(1))
        url = data.get("next")
    return out


def pypi_versions(name: str, get: Callable[[str], bytes] = _get) -> list[str]:
    data = json.loads(get(f"https://pypi.org/pypi/{name}/json"))
    return [v for v, files in (data.get("releases") or {}).items()
            if files and not all(f.get("yanked") for f in files)]


def maven_versions(coord: str, get: Callable[[str], bytes] = _get) -> list[str]:
    group, artifact = coord.split("/", 1)
    root = ET.fromstring(get(f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/maven-metadata.xml"))
    return [v.text for v in root.iter("version") if v.text]


def latest(source: dict, get: Callable[[str], bytes] = _get) -> Optional[str]:
    if "docker" in source:
        found = docker_tags(source["docker"], source["tags"], get)
    elif "pypi" in source:
        found = pypi_versions(source["pypi"], get)
    elif "maven" in source:
        found = maven_versions(source["maven"], get)
    else:
        raise ValueError(f"unknown source {source}")
    found = [v for v in found if not PRE.search(v)]
    return max(found, key=version_key) if found else None


def report(pins: dict, get: Callable[[str], bytes] = _get) -> list[dict]:
    rows = []
    for name, p in pins.items():
        if name.startswith("_"):
            continue
        try:
            new = latest(p["source"], get)
            err = None
        except Exception as e:  # noqa: BLE001 - one source being down does not hide the others
            new, err = None, str(e)[:200]
        rows.append({"component": name, "pinned": p["pinned"], "latest": new, "error": err,
                     "newer": bool(new and version_key(new) > version_key(p["pinned"]))})
    return rows


def main(argv: list[str]) -> int:
    pins = json.load(open(PINS))
    rows = report(pins)
    if "--matrix" in argv:
        print(json.dumps({"include": [{k: r[k] for k in ("component", "pinned", "latest")} for r in rows if r["newer"]]}))
        return 0
    for r in rows:
        mark = "newer" if r["newer"] else ("unknown: " + r["error"] if r["error"] else "current")
        print(f"{r['component']:<14} pinned {r['pinned']:<10} latest {r['latest'] or '?':<10} {mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
