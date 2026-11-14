"""Conformance checks for a connector: run them in the connector's own CI (``cairn source check``, or
``assert_conformant`` in pytest) before it is installed.

What is checked:

* the example config validates against the connector's ``config_model``;
* ``test_connection`` returns ``(bool, str)`` and does not raise;
* ``datasets`` yields ``Dataset`` objects with unique names and, per dataset, unique field paths;
* lineage points at well-formed dataset urns;
* the catalog metadata has no ``null`` values (Cairn Core refuses them) and only known aspects;
* two runs give the same metadata (no timestamps or random ids in it);
* no secret from the config (``SecretStr`` values, and keys named like passwords or tokens) appears in it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from cairn.sdk.source import Dataset, Source, to_mcps

ASPECTS = {"datasetProperties", "schemaMetadata", "ownership", "globalTags", "upstreamLineage", "subTypes", "status"}
SECRET_KEY = re.compile(r"(password|secret|token|api_?key|private_?key|credential)", re.I)
URN = re.compile(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,.+,[A-Z]+\)$")


def _nulls(v: Any, path: str = "") -> list[str]:
    if v is None:
        return [path or "/"]
    if isinstance(v, dict):
        return [p for k, x in v.items() for p in _nulls(x, f"{path}/{k}")]
    if isinstance(v, list):
        return [p for i, x in enumerate(v) for p in _nulls(x, f"{path}/{i}")]
    return []


def _secrets(config: BaseModel) -> list[str]:
    out = []
    for name, value in config:
        if isinstance(value, SecretStr):
            out.append(value.get_secret_value())
        elif isinstance(value, str) and SECRET_KEY.search(name):
            out.append(value)
        elif isinstance(value, BaseModel):
            out += _secrets(value)
    return [s for s in out if len(s) >= 4]


def check(source: Source, example_config: dict) -> dict:
    """``{"ok": bool, "problems": [...], "datasets": n}`` for one connector and an example config."""
    problems: list[str] = []
    if not source.platform or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source.platform):
        problems.append("platform must be a short lower-case name, e.g. csv")
    try:
        config = source.config_model.model_validate(example_config)
    except ValidationError as e:
        return {"ok": False, "problems": [f"the example config does not validate: {e.errors()[0]['msg']}"], "datasets": 0}
    try:
        result = source.test_connection(config)
        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool) and isinstance(result[1], str)):
            problems.append("test_connection must return (bool, str)")
    except Exception as e:  # noqa: BLE001 - reported, not raised
        problems.append(f"test_connection raised {type(e).__name__}: {e}")

    def run() -> list[Dataset]:
        items = list(source.datasets(config))
        bad = [type(d).__name__ for d in items if not isinstance(d, Dataset)]
        if bad:
            raise TypeError(f"datasets yielded {bad[0]}, not cairn.sdk.Dataset")
        return items

    try:
        first, second = run(), run()
    except Exception as e:  # noqa: BLE001
        problems.append(f"datasets raised {type(e).__name__}: {e}")
        return {"ok": False, "problems": problems, "datasets": 0}
    names = [d.name for d in first]
    if len(names) != len(set(names)):
        problems.append("dataset names repeat: " + ", ".join(sorted({n for n in names if names.count(n) > 1})[:5]))
    for d in first:
        paths = [f.path for f in d.fields]
        if len(paths) != len(set(paths)):
            problems.append(f"{d.name}: field paths repeat")
        for u in d.upstreams:
            if not URN.match(u.upstream):
                problems.append(f"{d.name}: lineage upstream {u.upstream!r} is not a dataset urn")
    mcps = to_mcps(source.platform, first)
    for m in mcps:
        if m["aspectName"] not in ASPECTS:
            problems.append(f"unknown aspect {m['aspectName']}")
        if nulls := _nulls(json.loads(m["aspect"]["value"])):
            problems.append(f"{m['entityUrn']} {m['aspectName']}: null at {nulls[0]}")
    if json.dumps(mcps, sort_keys=True) != json.dumps(to_mcps(source.platform, second), sort_keys=True):
        problems.append("two runs gave different metadata: keep timestamps and random ids out of it")
    dump = json.dumps(mcps)
    for secret in _secrets(config):
        if secret in dump:
            problems.append("a secret from the config appears in the metadata")
            break
    return {"ok": not problems, "problems": problems, "datasets": len(first)}


def assert_conformant(source: Source, example_config: dict) -> None:
    """For a connector's own pytest suite."""
    report = check(source, example_config)
    assert report["ok"], "\n".join(report["problems"])
