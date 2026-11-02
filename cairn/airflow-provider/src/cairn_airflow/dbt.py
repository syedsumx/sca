"""A dbt project as Airflow tasks: one task per model, seed and snapshot, wired as dbt's DAG.

Each task runs ``cairn dbt build --select <node>`` (the node and its tests) as the ``cairn`` connection's
user, so a failed model can be retried alone, its tests gate what depends on it, and each run is
recorded in Cairn. The graph comes from the project's ``manifest.json``, produced ahead of time
(``dbt parse`` when the project's image is built, or in CI), so the DAG parses without running dbt::

    from cairn_airflow.dbt import CairnDbtTaskGroup

    with DAG("shop_daily", schedule="@daily"):
        shop = CairnDbtTaskGroup(group_id="shop", project_dir="/opt/dbt/shop")

``select`` limits the nodes (by name, tag ``tag:x`` or path ``path:models/marts``, matched on the
manifest); other keyword arguments go to every :class:`~cairn_airflow.operators.CairnDbtOperator`.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Sequence

from airflow.utils.task_group import TaskGroup

from cairn_airflow.operators import CairnDbtOperator

KINDS = ("model", "seed", "snapshot")


def _selected(node: dict, select: Optional[Sequence[str]]) -> bool:
    if not select:
        return True
    for s in select:
        if s.startswith("tag:") and s[4:] in (node.get("tags") or []):
            return True
        if s.startswith("path:") and (node.get("original_file_path") or "").startswith(s[5:]):
            return True
        if s == node.get("name"):
            return True
    return False


def plan(manifest: dict, select: Optional[Sequence[str]] = None) -> dict[str, dict]:
    """The nodes to run, by unique id: their selector and the selected nodes they depend on."""
    nodes = {uid: n for uid, n in (manifest.get("nodes") or {}).items()
             if uid.split(".")[0] in KINDS and _selected(n, select)}
    out = {}
    for uid, n in nodes.items():
        name = n.get("name") or uid.split(".")[-1]
        version = n.get("version")
        # Its fully qualified name selects exactly this node (package, folders, name, version).
        selector = "fqn:" + ".".join(n["fqn"]) if n.get("fqn") else name
        out[uid] = {"name": name if version is None else f"{name}_v{version}", "selector": selector,
                    "upstream": sorted(d for d in (n.get("depends_on") or {}).get("nodes") or [] if d in nodes)}
    return out


class CairnDbtTaskGroup(TaskGroup):
    def __init__(self, *, group_id: str, project_dir: str, manifest_path: Optional[str] = None,
                 select: Optional[Sequence[str]] = None, command: str = "build", **operator_kwargs):
        super().__init__(group_id=group_id)
        path = manifest_path or os.path.join(project_dir, "target", "manifest.json")
        with open(path) as f:
            manifest = json.load(f)
        steps = plan(manifest, select)
        if not steps:
            raise ValueError(f"{group_id}: no dbt models, seeds or snapshots selected in {path}")
        tasks = {}
        with self:
            for uid, step in sorted(steps.items()):
                tasks[uid] = CairnDbtOperator(task_id=step["name"], project_dir=project_dir, command=command,
                                              select=step["selector"], **operator_kwargs)
        for uid, step in steps.items():
            for up in step["upstream"]:
                tasks[up] >> tasks[uid]
