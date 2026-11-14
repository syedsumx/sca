"""Builds the example plugins from their sources: the skills (examples/skills), the Copilot files and MCP
configuration `cairn ide init` writes (cairn.ai.ide). Run it after changing either; tests/test_examples.py fails when
the plugins here differ from what it builds.

    python examples/plugins/build.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(EXAMPLES, "..", "src"))

from cairn.ai import ide  # noqa: E402

URL = "https://cairn.example.com"


def _version() -> str:
    """Cairn's own version: the plugin moves with each release, so installed copies are offered the update."""
    text = open(os.path.join(EXAMPLES, "..", "pyproject.toml")).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


VERSION = _version()


def skills() -> dict[str, str]:
    root = os.path.join(EXAMPLES, "skills")
    return {n: open(os.path.join(root, n, "SKILL.md")).read() for n in sorted(os.listdir(root))
            if os.path.isfile(os.path.join(root, n, "SKILL.md"))}


def claude_code() -> dict[str, str]:
    """A Claude Code plugin: Cairn's MCP server, the skills, and the three agents as subagents."""
    files = {".claude-plugin/plugin.json": json.dumps({
        "name": "cairn", "version": VERSION, "description": "Work with Cairn, a governed data platform: build pipelines, "
        "check quality, govern data and change configuration as code, through Cairn's MCP server.",
        "author": {"name": "Cairn"}, "keywords": ["data", "governance", "mcp", "lakehouse"]}, indent=2) + "\n",
        ".mcp.json": json.dumps({"mcpServers": {"cairn": {
            "type": "http", "url": "${CAIRN_URL}/cairn/mcp/mcp?toolsets=catalog,pipelines,quality,iac"}}},
            indent=2) + "\n"}
    for name, text in skills().items():
        files[f"skills/{name}/SKILL.md"] = text
    for name, (desc, body) in ide.AGENTS.items():
        files[f"agents/{name}.md"] = (f"---\nname: {name}\ndescription: {json.dumps(desc)}\n---\n\n"
                                      + body.format(working=ide.WORKING))
    return files


def copilot() -> dict[str, str]:
    """What `cairn ide init --client vscode,copilot-agent` writes, and the skills as Copilot agent skills."""
    with tempfile.TemporaryDirectory() as d:
        ide.init(d, ["vscode", "copilot-agent"], ide.mcp_url(URL))
        files = {}
        for base, _, names in os.walk(d):
            for n in names:
                path = os.path.join(base, n)
                files[os.path.relpath(path, d)] = open(path).read()
    for name, text in skills().items():
        files[f".github/skills/{name}/SKILL.md"] = text
    return files


def build() -> dict[str, str]:
    out = {f"claude-code/cairn/{k}": v for k, v in claude_code().items()}
    out.update({f"copilot/{k}": v for k, v in copilot().items()})
    out[".claude-plugin/marketplace.json"] = json.dumps({
        "name": "cairn-examples", "owner": {"name": "Cairn"},
        "plugins": [{"name": "cairn", "source": "./claude-code/cairn", "version": VERSION,
                     "description": "Cairn's MCP server, skills and agents for Claude Code"}]}, indent=2) + "\n"
    return out


def main() -> None:
    for sub in ("claude-code", "copilot", ".claude-plugin"):
        shutil.rmtree(os.path.join(HERE, sub), ignore_errors=True)
    files = build()
    for rel, text in files.items():
        path = os.path.join(HERE, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    print(f"built {len(files)} files")


if __name__ == "__main__":
    main()
