"""The agent kit's library (L164): skills, agents and commands for the AI coding tools people use on Cairn work,
written into a repository by ``cairn ide init --profile minimal|standard|full``.

* **What is in it.** Skills (``SKILL.md``, the format Claude Code, GitHub Copilot's agent skills and Codex read) and
  agents, kept as files in ``cairn/agentkit/kit/``. Every tool a skill names exists on Cairn's MCP server, and
  everything scans clean with ``cairn agents scan`` (tests check both).
* **Profiles.** ``minimal``: finding data and changing Cairn as code. ``standard`` adds pipelines, data quality,
  governance review, SQL, and the three working agents. ``full`` adds impact review, access requests, incident
  triage, agent safety, the two reviewing agents and the commands.
* **Where each tool reads them.** Claude Code: ``.claude/skills/<name>/SKILL.md``, ``.claude/agents/<name>.md``,
  ``.claude/commands/<name>.md``. GitHub Copilot (VS Code and the coding agent): ``.github/skills/<name>/SKILL.md``
  and ``.github/agents/<name>.agent.md``. Cursor: ``.cursor/rules/<name>.mdc`` (applied when its description fits).
  Codex: ``.agents/skills/<name>/SKILL.md`` and Cairn's section of ``AGENTS.md``.
* **What it may touch.** Only its own files, inside the repository (:func:`safe_path` refuses a path that leaves it
  or passes through a link). ``.cairn/agent-kit.json`` records what was written and each file's hash, so a later run
  updates the files nobody edited, keeps the ones somebody did (``--force`` replaces them), and removes its own
  unedited files that a smaller profile no longer includes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from importlib import resources
from typing import Callable, Optional

PROFILES = ("minimal", "standard", "full")
MANIFEST = ".cairn/agent-kit.json"
VERSION = 1

# Each skill and agent, and the smallest profile that includes it.
SKILLS = {
    "cairn-find-data": "minimal", "cairn-iac-change": "minimal",
    "cairn-build-pipeline": "standard", "cairn-data-quality": "standard", "cairn-governance-review": "standard",
    "cairn-sql": "standard",
    "cairn-impact-review": "full", "cairn-access-request": "full", "cairn-incident-triage": "full",
    "cairn-agent-safety": "full",
}
AGENTS = {
    "cairn-data-engineer": "standard", "cairn-steward": "standard", "cairn-platform-admin": "standard",
    "cairn-change-reviewer": "full", "cairn-agent-security-reviewer": "full",
}
# Agents that run a command (the scan); the others only read files and call Cairn.
RUNS_COMMANDS = {"cairn-agent-security-reviewer"}
# Where the kit's own files live: nothing outside these is ever written or removed as part of the library.
OWNED = (".claude/skills/", ".claude/agents/", ".claude/commands/", ".github/skills/", ".github/agents/",
         ".cursor/rules/", ".agents/skills/")


# Each client's part of them: a run for some clients leaves the other clients' files (and their record) alone.
CLIENT_PATHS = {"claude-code": (".claude/skills/", ".claude/agents/", ".claude/commands/"),
                "vscode": (".github/skills/", ".github/agents/"), "copilot-agent": (".github/skills/", ".github/agents/"),
                "cursor": (".cursor/rules/",), "codex": (".agents/skills/",)}


class LibraryError(ValueError):
    pass


def _in(profile: str, smallest: str) -> bool:
    return PROFILES.index(profile) >= PROFILES.index(smallest)


def _read(*parts: str) -> str:
    return resources.files("cairn.agentkit").joinpath("kit", *parts).read_text(encoding="utf-8")


def _split(text: str) -> tuple[dict[str, str], str]:
    """A file's front matter (``key: value`` lines; values JSON strings or plain) and its body."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise LibraryError("a library file has no front matter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        meta[key.strip()] = json.loads(value) if value.startswith('"') else value
    return meta, m.group(2).lstrip("\n")


def skill(name: str) -> str:
    return _read("skills", name, "SKILL.md")


def agent(name: str) -> tuple[str, str]:
    """An agent's (description, instructions)."""
    from cairn.ai import ide

    if name in ide.AGENTS:
        desc, body = ide.AGENTS[name]
    else:
        meta, body = _split(_read("agents", f"{name}.md"))
        desc = meta["description"]
    return desc, body.replace("{working}", ide.WORKING)


def skills_in(profile: str) -> list[str]:
    return [n for n, p in SKILLS.items() if _in(profile, p)]


def agents_in(profile: str) -> list[str]:
    return [n for n, p in AGENTS.items() if _in(profile, p)]


# ---- each tool's form of a skill, an agent, a command ------------------------------------------------------------
def _claude_agent(name: str) -> str:
    from cairn.ai import ide

    desc, body = agent(name)
    tools = "Read, Grep, Glob, " + ("Bash, " if name in RUNS_COMMANDS else "") + f"mcp__{ide.SERVER}__*"
    return f"---\nname: {name}\ndescription: {json.dumps(desc)}\ntools: {tools}\n---\n\n{body}"


def _copilot_agent(name: str) -> str:
    from cairn.ai import ide

    if name in ide.AGENTS:
        return ide.agent_file(name)
    desc, body = agent(name)
    tools = [f"{ide.SERVER}/*"] + (["runCommands"] if name in RUNS_COMMANDS else [])
    listed = ", ".join(f"'{t}'" for t in tools)
    return f"---\nname: {name}\ndescription: {json.dumps(desc)}\ntools: [{listed}]\n---\n\n{body}"


def _cursor_rule(name: str) -> str:
    meta, body = _split(skill(name))
    return f"---\ndescription: {json.dumps(meta['description'])}\nalwaysApply: false\n---\n\n{body}"


def _claude_command(name: str) -> str:
    from cairn.ai import ide

    desc, body = ide.PROMPTS[name]
    asked = re.sub(r"\$\{input:[^:}]+:([^}]+)\}", r"<\1>", body)
    return f"---\ndescription: {json.dumps(desc)}\n---\n\n{asked}\n\nWhat was asked: $ARGUMENTS\n"


def files_for(clients: list[str], profile: str) -> dict[str, str]:
    """``{path: content}`` of the library for these clients at this profile."""
    from cairn.ai import ide

    if profile not in PROFILES:
        raise LibraryError(f"--profile is one of {', '.join(PROFILES)}")
    out: dict[str, str] = {}
    skills, agents = skills_in(profile), agents_in(profile)
    if "claude-code" in clients:
        out.update({f".claude/skills/{n}/SKILL.md": skill(n) for n in skills})
        out.update({f".claude/agents/{n}.md": _claude_agent(n) for n in agents})
        if profile == "full":
            out.update({f".claude/commands/{n}.md": _claude_command(n) for n in ide.PROMPTS})
    if "vscode" in clients or "copilot-agent" in clients:
        out.update({f".github/skills/{n}/SKILL.md": skill(n) for n in skills})
        out.update({f".github/agents/{n}.agent.md": _copilot_agent(n) for n in agents})
    if "cursor" in clients:
        out.update({f".cursor/rules/{n}.mdc": _cursor_rule(n) for n in skills})
    if "codex" in clients:
        out.update({f".agents/skills/{n}/SKILL.md": skill(n) for n in skills})
    return out


# ---- writing only inside the repository, and only the kit's own files ------------------------------------------
def safe_path(root: str, rel: str) -> str:
    """``rel`` under ``root`` as a path, refused when it would leave the repository: an absolute path, ``..``, or a
    link anywhere along it (a linked ``.claude`` would put Cairn's files wherever it points)."""
    parts = rel.split("/")
    if not rel or os.path.isabs(rel) or any(p in ("", ".", "..") for p in parts) or "\\" in rel or "\x00" in rel:
        raise LibraryError(f"{rel!r} is not a path inside the repository")
    path = root
    for i, part in enumerate(parts):
        path = os.path.join(path, part)
        if i < len(parts) - 1 and os.path.lexists(path) and not os.path.islink(path) and not os.path.isdir(path):
            where = os.path.relpath(path, root).replace(os.sep, "/")
            raise LibraryError(f"{where} is a file where Cairn needs a folder: move it aside")
        if os.path.islink(path):
            where = os.path.relpath(path, root).replace(os.sep, "/")
            raise LibraryError(f"{where} is a link: Cairn writes only to its own files inside the repository "
                               "(replace the link with the real file or folder, or add Cairn by hand)")
    return path


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _owned(rel: str) -> bool:
    return rel.startswith(OWNED) and ".." not in rel.split("/")


def load_manifest(text: Optional[str]) -> dict[str, str]:
    """``{path: hash}`` of what an earlier run wrote. The manifest is a file in the repository, so anyone can edit
    it: only the kit's own paths are believed, and a file is touched only when its content still has the hash."""
    if not text:
        return {}
    try:
        doc = json.loads(text)
        files = doc.get("files", {}) if isinstance(doc, dict) else {}
    except ValueError:
        return {}
    if not isinstance(files, dict):
        return {}
    return {rel: h for rel, h in files.items() if isinstance(rel, str) and isinstance(h, str) and _owned(rel)}


def _recorded_clients(text: Optional[str]) -> list[str]:
    try:
        got = json.loads(text or "{}").get("clients", [])
    except (ValueError, AttributeError):
        return []
    return [c for c in got if isinstance(c, str)] if isinstance(got, list) else []


def plan(wanted: dict[str, str], read: Callable[[str], Optional[str]], force: bool, profile: str,
         clients: list[str]) -> dict:
    """What a run does with the library: ``write`` (``{path: content}``, the manifest included), ``kept`` (files a
    person edited or wrote themselves, left as they are), ``remove`` (the kit's unedited files this profile no
    longer includes) and ``same`` (already as wanted)."""
    before = load_manifest(read(MANIFEST))
    write: dict[str, str] = {}
    kept, same, remove = [], [], []
    after: dict[str, str] = {}
    for rel, content in wanted.items():
        have = read(rel)
        if have is None:
            write[rel] = content
        elif have == content:
            same.append(rel)
        elif force or before.get(rel) == digest(have):          # ours and unedited: a newer version replaces it
            write[rel] = content
        else:
            kept.append(rel)
            continue                                              # theirs now: not recorded, never removed by us
        after[rel] = digest(content)
    mine = tuple(p for c in clients for p in CLIENT_PATHS.get(c, ()))
    for rel, h in before.items():
        if rel in wanted:
            continue
        have = read(rel)
        if not rel.startswith(mine):                              # another client's, from an earlier run: as it was
            if have is not None:
                after[rel] = h
        elif have is not None and digest(have) == h:
            remove.append(rel)
    earlier = [c for c in _recorded_clients(read(MANIFEST)) if c in CLIENT_PATHS and c not in clients
               and any(rel.startswith(CLIENT_PATHS[c]) for rel in after)]
    recorded = sorted({c for c in clients if c in CLIENT_PATHS} | set(earlier))
    doc = {"version": VERSION, "profile": profile, "clients": recorded, "files": dict(sorted(after.items()))}
    manifest = json.dumps(doc, indent=2) + "\n"
    if read(MANIFEST) != manifest:
        write[MANIFEST] = manifest
    return {"write": write, "kept": kept, "remove": remove, "same": same}


__all__ = ["AGENTS", "LibraryError", "MANIFEST", "PROFILES", "SKILLS", "agent", "agents_in", "files_for",
           "load_manifest", "plan", "safe_path", "skill", "skills_in"]
