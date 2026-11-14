"""The agent kit's library (L164): ``cairn ide init --profile`` writes skills, agents and commands for Claude Code,
GitHub Copilot, Cursor and Codex, run as the CLI runs (a process) into a real folder; it writes only inside the
repository, keeps what people edited, and everything it writes names real Cairn tools and scans clean."""

import asyncio
import json
import os
import subprocess
import sys
import tomllib

import pytest
import yaml

from cairn.agentkit import library, scan
from cairn.ai import ide

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
URL = "https://cairn.example.com"


def cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cairn.cli", "ide", "init", "--url", URL, *args], capture_output=True,
                          text=True, env={**os.environ, "PYTHONPATH": SRC}, timeout=120)


def tree(root) -> dict[str, str]:
    return {str(p.relative_to(root)).replace(os.sep, "/"): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def front_matter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_each_profile_writes_its_library_in_every_tools_own_place(tmp_path):
    sets = {}
    for profile in library.PROFILES:
        root = tmp_path / profile
        root.mkdir()
        r = cli("--client", "claude-code,vscode,cursor,codex", "--profile", profile, "--dir", str(root))
        assert r.returncode == 0, r.stderr
        sets[profile] = tree(root)
    minimal, standard, full = (set(sets[p]) for p in library.PROFILES)
    assert minimal < standard < full
    names = lambda files, head, tail: sorted(f[len(head):-len(tail)] for f in files if f.startswith(head) and f.endswith(tail))  # noqa: E731
    assert names(minimal, ".claude/skills/", "/SKILL.md") == ["cairn-find-data", "cairn-iac-change"]
    assert not names(minimal, ".claude/agents/", ".md")
    assert len(names(standard, ".claude/skills/", "/SKILL.md")) == 7 and len(names(standard, ".claude/agents/", ".md")) == 3
    assert len(names(full, ".claude/skills/", "/SKILL.md")) == 11 and len(names(full, ".claude/agents/", ".md")) == 5
    assert names(full, ".claude/commands/", ".md") == sorted(ide.PROMPTS) and not names(standard, ".claude/commands/", ".md")
    # The same skills for each tool, in the place it reads.
    for head, tail in ((".github/skills/", "/SKILL.md"), (".agents/skills/", "/SKILL.md"), (".cursor/rules/", ".mdc")):
        assert names(full, head, tail) == names(full, ".claude/skills/", "/SKILL.md"), head
    assert names(full, ".github/agents/", ".agent.md") == names(full, ".claude/agents/", ".md")

    files = sets["full"]
    for rel, text in files.items():
        if rel.endswith("SKILL.md"):
            fm = front_matter(text)
            assert fm["name"] == rel.split("/")[-2] and len(fm["description"]) > 60, rel
        elif rel.startswith(".claude/agents/"):
            fm = front_matter(text)
            tools = [t.strip() for t in fm["tools"].split(",")]
            assert fm["name"] == rel.split("/")[-1][:-3] and "mcp__cairn__*" in tools and "*" not in tools, rel
            assert ("Bash" in tools) == (fm["name"] == "cairn-agent-security-reviewer")     # only the one that scans
            assert "Write" not in tools and "Edit" not in tools
            assert "{working}" not in text and "as the person signed in" in text
        elif rel.startswith(".cursor/rules/"):
            fm = front_matter(text)
            assert fm["alwaysApply"] is False and len(fm["description"]) > 60 and "globs" not in fm, rel
        elif rel.startswith(".claude/commands/"):
            assert front_matter(text)["description"] and "$ARGUMENTS" in text and "${input" not in text, rel
    codex = tomllib.loads(files[".codex/config.toml"])
    assert codex == {"mcp_servers": {"cairn": {"url": f"{URL}/cairn/mcp/mcp?toolsets=catalog,pipelines,quality,iac"}}}
    assert files["AGENTS.md"].count(ide.BEGIN) == 1 and "iac_propose" in files["AGENTS.md"]
    manifest = json.loads(files[library.MANIFEST])
    assert manifest["profile"] == "full" and manifest["clients"] == ["claude-code", "codex", "cursor", "vscode"]
    assert set(manifest["files"]) == {f for f in full if f.startswith(library.OWNED)}
    assert all(library.digest(files[f]) == h for f, h in manifest["files"].items())
    # Nothing written holds a credential, and all of it scans clean.
    for profile in library.PROFILES:
        assert scan.scan_path(str(tmp_path / profile)) == [], profile


def test_a_later_run_updates_its_own_files_keeps_edited_ones_and_removes_what_a_smaller_profile_drops(tmp_path):
    args = ("--client", "claude-code,cursor", "--dir", str(tmp_path))
    assert cli(*args, "--profile", "full").returncode == 0
    first = tree(tmp_path)
    r = cli(*args, "--profile", "full")                                            # again: nothing changes
    assert r.returncode == 0 and tree(tmp_path) == first and "kept" not in r.stdout and "removed" not in r.stdout

    edited = tmp_path / ".claude/skills/cairn-sql/SKILL.md"                        # a person's edit
    edited.write_text(first[".claude/skills/cairn-sql/SKILL.md"] + "\nOur team: always qualify with the schema.\n")
    mine = tmp_path / ".claude/skills/our-own/SKILL.md"                            # a person's own skill
    mine.parent.mkdir()
    mine.write_text("---\nname: our-own\ndescription: ours\n---\nOurs.\n")
    stale = tmp_path / ".cursor/rules/cairn-find-data.mdc"                         # ours, from an older release
    stale.write_text("---\ndescription: \"older\"\nalwaysApply: false\n---\n\nOlder text.\n")
    manifest = json.loads((tmp_path / library.MANIFEST).read_text())
    manifest["files"][".cursor/rules/cairn-find-data.mdc"] = library.digest(stale.read_text())
    (tmp_path / library.MANIFEST).write_text(json.dumps(manifest))

    r = cli(*args, "--profile", "full")
    assert r.returncode == 0, r.stderr
    assert "kept .claude/skills/cairn-sql/SKILL.md" in r.stdout and "Our team" in edited.read_text()
    assert stale.read_text() == first[".cursor/rules/cairn-find-data.mdc"]          # updated: nobody had edited it
    assert ".claude/skills/cairn-sql/SKILL.md" not in json.loads((tmp_path / library.MANIFEST).read_text())["files"]

    r = cli(*args, "--profile", "minimal")                                         # a smaller profile
    assert r.returncode == 0, r.stderr
    left = tree(tmp_path)
    assert "removed .claude/skills/cairn-incident-triage/SKILL.md" in r.stdout
    assert not (tmp_path / ".claude/skills/cairn-incident-triage").exists()        # its folder too
    assert sorted(f for f in left if f.startswith(".claude/")) == [
        ".claude/skills/cairn-find-data/SKILL.md", ".claude/skills/cairn-iac-change/SKILL.md",
        ".claude/skills/cairn-sql/SKILL.md", ".claude/skills/our-own/SKILL.md"]     # the edited one and theirs stay
    assert sorted(f for f in left if f.startswith(".cursor/rules/")) == [
        ".cursor/rules/cairn-find-data.mdc", ".cursor/rules/cairn-iac-change.mdc"]

    r = cli(*args, "--profile", "standard", "--force")                             # --force replaces the edited one
    assert r.returncode == 0 and "Our team" not in edited.read_text() and mine.exists()


def test_a_run_for_one_tool_leaves_another_tools_library_alone(tmp_path):
    assert cli("--client", "claude-code", "--profile", "full", "--dir", str(tmp_path)).returncode == 0
    claude = {f: t for f, t in tree(tmp_path).items() if f.startswith(".claude/")}
    r = cli("--client", "cursor", "--profile", "minimal", "--dir", str(tmp_path))
    assert r.returncode == 0 and "removed" not in r.stdout, r.stdout + r.stderr
    after = tree(tmp_path)
    assert {f: t for f, t in after.items() if f.startswith(".claude/")} == claude
    manifest = json.loads(after[library.MANIFEST])
    assert manifest["clients"] == ["claude-code", "cursor"] and set(claude) <= set(manifest["files"])
    r = cli("--client", "claude-code", "--profile", "minimal", "--dir", str(tmp_path))   # still its own to shrink
    assert r.returncode == 0 and "removed .claude/agents/cairn-steward.md" in r.stdout
    assert sorted(f for f in tree(tmp_path) if f.startswith(".cursor/rules/")) == [
        ".cursor/rules/cairn-find-data.mdc", ".cursor/rules/cairn-iac-change.mdc"]
    (tmp_path / ".agents").write_text("a file, not a folder\n")
    r = cli("--client", "codex", "--profile", "minimal", "--dir", str(tmp_path))
    assert r.returncode != 0 and ".agents is a file where Cairn needs a folder" in r.stderr
    assert not (tmp_path / ".codex").exists()                                          # refused before any write


def test_it_writes_only_inside_the_repository(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    repo = tmp_path / "repo"
    repo.mkdir()
    os.symlink(outside, repo / ".claude")                                           # a linked folder
    r = cli("--client", "claude-code,cursor", "--profile", "full", "--dir", str(repo))
    assert r.returncode != 0 and ".claude is a link" in r.stderr
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert sorted(p.name for p in repo.iterdir()) == [".claude"]                    # refused before anything was written
    os.remove(repo / ".claude")

    (repo / ".cursor/rules").mkdir(parents=True)
    os.symlink(outside / "keep.txt", repo / ".cursor/rules/cairn-find-data.mdc")    # a linked file
    r = cli("--client", "cursor", "--profile", "minimal", "--dir", str(repo))
    assert r.returncode != 0 and "is a link" in r.stderr and (outside / "keep.txt").read_text() == "keep"
    os.remove(repo / ".cursor/rules/cairn-find-data.mdc")

    # The manifest is a file anyone can edit: it cannot make Cairn remove something that is not the kit's own.
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("print('app')\n")
    (repo / "notes.md").write_text("notes\n")
    (repo / ".cairn").mkdir()
    (repo / ".cairn/agent-kit.json").write_text(json.dumps({"version": 1, "files": {
        "src/app.py": library.digest("print('app')\n"), "notes.md": library.digest("notes\n"),
        "../outside/keep.txt": library.digest("keep"), ".claude/skills/../../notes.md": library.digest("notes\n"),
        "/etc/hostname": "0" * 64}}))
    r = cli("--client", "claude-code", "--profile", "minimal", "--dir", str(repo))
    assert r.returncode == 0, r.stderr
    assert (repo / "src/app.py").exists() and (repo / "notes.md").exists() and (outside / "keep.txt").exists()
    assert "removed" not in r.stdout
    for bad in ("../x", "/abs", "a/../b", "a//b", ""):
        with pytest.raises(library.LibraryError):
            library.safe_path(str(repo), bad)


def test_codex_configuration_keeps_the_rest_of_the_file(tmp_path):
    cfg = tmp_path / ".codex/config.toml"
    cfg.parent.mkdir()
    theirs = '# our settings\nmodel = "gpt"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"   # pinned elsewhere\n'
    cfg.write_text(theirs)
    (tmp_path / "AGENTS.md").write_text("# Our rules\n\nUse tabs.\n")
    assert cli("--client", "codex", "--dir", str(tmp_path)).returncode == 0
    text = cfg.read_text()
    assert text.startswith(theirs) and tomllib.loads(text)["mcp_servers"]["cairn"]["url"].startswith(URL)
    assert tomllib.loads(text)["mcp_servers"]["docs"] == {"command": "docs-mcp"}
    agents = (tmp_path / "AGENTS.md").read_text()
    assert agents.startswith("# Our rules\n\nUse tabs.\n") and agents.count(ide.BEGIN) == 1
    assert cli("--client", "codex", "--dir", str(tmp_path)).returncode == 0          # again: the same file
    assert cfg.read_text() == text and (tmp_path / "AGENTS.md").read_text() == agents

    r = cli("--client", "codex", "--toolsets", "catalog", "--dir", str(tmp_path))    # a different URL
    assert r.returncode != 0 and "--force replaces it" in r.stderr and cfg.read_text() == text
    assert cli("--client", "codex", "--toolsets", "catalog", "--dir", str(tmp_path), "--force").returncode == 0
    doc = tomllib.loads(cfg.read_text())
    assert doc["mcp_servers"]["cairn"] == {"url": f"{URL}/cairn/mcp/mcp?toolsets=catalog"}
    assert doc["model"] == "gpt" and doc["mcp_servers"]["docs"] == {"command": "docs-mcp"}
    assert "# our settings" in cfg.read_text() and "# pinned elsewhere" in cfg.read_text()

    cfg.write_text("[mcp_servers\nbroken")
    r = cli("--client", "codex", "--dir", str(tmp_path))
    assert r.returncode != 0 and "not TOML" in r.stderr and cfg.read_text() == "[mcp_servers\nbroken"
    cfg.write_text('mcp_servers = { cairn = { url = "https://other.example.com/mcp" } }\n')   # an inline table
    r = cli("--client", "codex", "--dir", str(tmp_path), "--force")
    assert r.returncode != 0 and "by hand" in r.stderr


def test_the_library_names_only_real_tools_and_matches_the_examples(settings, graph):
    from cairn.ai.mcp_server import build_mcp_server
    from cairn.services import build_services

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    server, _ = build_mcp_server(s)
    tools = {t.name for t in asyncio.run(server.list_tools())} | set(server._tool_manager._tools)
    import re

    named = lambda text: set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))  # noqa: E731
    for name in library.SKILLS:
        text = library.skill(name)
        assert named(text), name
        assert not named(text) - tools, (name, named(text) - tools)
        for other in re.findall(r"skill `(cairn-[a-z-]+)`", text):                  # a skill another one points to
            assert other in library.SKILLS and not (
                library.PROFILES.index(library.SKILLS[other]) > library.PROFILES.index(library.SKILLS[name])), (name, other)
    for name in library.AGENTS:
        desc, body = library.agent(name)
        assert len(desc) > 40 and not named(body) - tools, (name, named(body) - tools)
    # The four skills the examples also ship are the same text.
    for name in os.listdir(os.path.join(REPO, "examples", "skills")):
        path = os.path.join(REPO, "examples", "skills", name, "SKILL.md")
        if os.path.isfile(path):
            assert library.skill(name) == open(path).read(), name
    with pytest.raises(library.LibraryError):
        library.files_for(["claude-code"], "everything")
