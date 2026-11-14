#!/usr/bin/env python3
"""Writes ui/src/consumer/resources/agentExamples.generated.json from examples/agents/*: what Agents › Examples
offers to install (each agent's definition as POST /v1/custom-agents takes it, and how many cases it is tested on).
tests/test_examples.py fails when the file and the examples differ.

    python examples/agents/ui_examples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "ui" / "src" / "consumer" / "resources" / "agentExamples.generated.json"
FIELDS = ("name", "description", "instructions", "tools", "scopes", "max_steps")


def build() -> str:
    out = []
    for d in sorted(p for p in HERE.iterdir() if (p / "agent.yaml").exists()):
        agent = yaml.safe_load((d / "agent.yaml").read_text())
        cases = (yaml.safe_load((d / "evals.yaml").read_text()) or {}).get("cases") or [] \
            if (d / "evals.yaml").exists() else []
        out.append({"agent": {k: agent[k] for k in FIELDS if k in agent}, "cases": len(cases),
                    "source": f"examples/agents/{d.name}/agent.yaml"})
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(HERE.parents[1])} ({len(json.loads(OUT.read_text()))} agents)", file=sys.stderr)
