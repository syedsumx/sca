"""The agent security scanner (L164): what a repository tells AI coding agents, and what it lets them do, checked
before an agent reads it.

``cairn agents scan [PATH]`` reads the files AI tools take their setup from (MCP server configurations, agent and
skill definitions, repository instructions, tool settings and hooks) and reports what makes them unsafe:

=========================  ========  ==================================================================================
rule                       severity  what it finds
=========================  ========  ==================================================================================
``secret``                 critical  a credential in the file (keys, tokens, private keys, passwords); never echoed
``mcp-inline-credential``  critical  an MCP server's env or header holds a literal value for a secret-named setting
``remote-script``          critical  a download piped into a shell (``curl … | sh``) in a server command or a hook
``invisible-text``         high      invisible characters (Unicode tags, zero-width, bidi controls) in agent text
``injection``              high      text that reads like instructions to an AI (ignore previous instructions, …)
``mcp-plain-http``         high      an MCP server reached over plain http beyond this machine
``mcp-shell``              high      an MCP server started through a shell (``sh -c``, ``cmd /c``, ``powershell -c``)
``permissions-broad``      high      a tool allowed to run any command, or permission prompts switched off
``hook-network``           high      a hook that reaches the network (curl, wget, nc, a URL)
``mcp-unpinned``           medium    an MCP server's package or image not pinned to a version (``npx pkg``, ``:latest``)
``permissions-destructive``medium    a destructive command allowed without asking (``rm -rf``, force pushes, …)
``agent-all-tools``        medium    an agent definition given every tool (``tools: ['*']``)
``exfil-instruction``      medium    instructions to send or upload something to a URL beyond this machine
``hook``                   info      a hook runs a command (listed so it is reviewed)
``not-read``               info      a file too large, unreadable or not valid JSON (only checked for secrets)
=========================  ========  ==================================================================================

Each finding names the file and line, the rule, and how to fix it. The scanner reads only (it follows no link out of
the folder, skips files over 1 MB and never runs anything) and prints no secret's value. ``--fail-on`` (default
``high``) sets the exit code for CI; ``--format sarif`` writes SARIF 2.1.0 for code scanning.

The same checks run on what is registered in Cairn (:func:`check_agent`, :func:`check_mcp_server`): a custom agent's
instructions and an MCP server's address are refused at registration when they carry a finding at or above
``CAIRN_AGENT_SCAN_FAIL_ON`` (default ``high``), so an unsafe definition is never stored, fail closed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from cairn.ai.guardrails import _INSTRUCTIONS, _INVISIBLE, secret_kinds

SEVERITIES = ("info", "low", "medium", "high", "critical")
MAX_BYTES = 1_000_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".tox"}

# Where AI tools read their setup from (paths relative to the folder scanned; * is one path part, ** any number).
MCP_FILES = (".mcp.json", ".vscode/mcp.json", ".cursor/mcp.json", ".junie/mcp/mcp.json", ".github/copilot-mcp.json",
             "claude_desktop_config.json", ".gemini/settings.json", ".roo/mcp.json", ".amazonq/mcp.json")
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
CODEX_FILES = (".codex/config.toml",)
TEXT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".github/copilot-instructions.md",
              ".github/agents/*.agent.md", ".github/agents/*.md", ".github/prompts/*.prompt.md",
              ".github/instructions/*.instructions.md", ".claude/agents/*.md", ".claude/commands/*.md",
              ".claude/skills/**/SKILL.md", "skills/**/SKILL.md", ".cursor/rules/*.mdc", ".cursor/rules/*.md",
              ".windsurf/rules/*.md", ".junie/guidelines.md", ".clinerules", ".clinerules/*.md")
AGENT_FILES = (".github/agents/*.agent.md", ".github/agents/*.md", ".claude/agents/*.md")

FIX = {
    "secret": "remove it, rotate it, and read it from the environment or a secret store (e.g. \"${env:NAME}\")",
    "mcp-inline-credential": "reference an environment variable or input (\"${env:NAME}\", \"${input:name}\") instead",
    "remote-script": "download, check (pin a digest) and run a reviewed script, never a pipe into a shell",
    "invisible-text": "remove the invisible characters; they can carry instructions a reviewer cannot see",
    "injection": "remove the text or rephrase it as data; agents read it as instructions",
    "mcp-plain-http": "use https (plain http is only for this machine)",
    "mcp-shell": "start the server's program directly with its arguments, not through a shell",
    "permissions-broad": "allow the specific commands the agent needs (e.g. \"Bash(npm test)\") and keep prompts on",
    "permissions-destructive": "leave destructive commands to ask each time",
    "hook-network": "keep hooks local; a hook that reaches the network can send what the agent sees",
    "mcp-unpinned": "pin the version (npx pkg@1.2.3, uvx pkg==1.2.3, an image by @sha256 digest)",
    "agent-all-tools": "list the tools the agent needs",
    "exfil-instruction": "remove instructions to send data to a URL, or point them at a host you control",
    "hook": "review what the hook runs",
}


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    message: str
    fix: str = field(default="")

    def __post_init__(self) -> None:
        self.fix = self.fix or FIX.get(self.rule, "")


def at_least(severity: str, threshold: str) -> bool:
    return SEVERITIES.index(severity) >= SEVERITIES.index(threshold)


# ---- text: instructions, invisible characters, secrets, exfiltration ------------------------------------------------
_LOCAL = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|0\.0\.0\.0|host\.docker\.internal)$", re.I)
_EXFIL = re.compile(r"\b(?:curl|wget|fetch|post|send|upload|forward|report|exfiltrate)\b[^\n]{0,120}?"
                    r"(https?://[^\s\"'`)<>]+)", re.I)
_REMOTE_SCRIPT = re.compile(r"\b(?:curl|wget|iwr|Invoke-WebRequest)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b|"
                            r"\b(?:iex|Invoke-Expression)\b[^\n]*\b(?:iwr|Invoke-WebRequest|DownloadString)\b", re.I)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _local(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(_LOCAL.match(host))


def _secrets(text: str, file: str) -> list[Finding]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind in secret_kinds(line):
            # "${env:...}" and "$VAR" references are where a secret should come from, not secrets.
            if kind == "password_assignment" and re.search(r"[:=]\s*[\"']?(?:\$\{|\$[A-Z_]|<[^>]+>|\*{3,}|x{6,})", line, re.I):
                continue
            out.append(Finding("secret", "critical", file, n, f"a credential ({kind.replace('_', ' ')}) is written here"))
    return out


def scan_text(text: str, file: str, *, instructions: bool = True) -> list[Finding]:
    """Text an agent reads (instructions, prompts, skills, agent definitions)."""
    out = _secrets(text, file)
    for m in _INVISIBLE.finditer(text):
        out.append(Finding("invisible-text", "high", file, _line_of(text, m.start()),
                           f"an invisible character (U+{ord(m.group()):04X})"))
        break                                   # one is enough to make the point; the fix removes them all
    if instructions:
        seen: set[int] = set()
        for m in _INSTRUCTIONS.finditer(text):
            n = _line_of(text, m.start())
            if n not in seen:
                seen.add(n)
                out.append(Finding("injection", "high", file, n, f"reads like instructions to an AI: \"{m.group()[:60]}\""))
        for m in _EXFIL.finditer(text):
            if not _local(m.group(1)):
                out.append(Finding("exfil-instruction", "medium", file, _line_of(text, m.start()),
                                   f"asks to send something to {urlsplit(m.group(1)).hostname}"))
    for m in _REMOTE_SCRIPT.finditer(text):
        out.append(Finding("remote-script", "critical", file, _line_of(text, m.start()), "a download piped into a shell"))
    return out


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def scan_agent_definition(text: str, file: str) -> list[Finding]:
    out = scan_text(text, file)
    tools = _frontmatter(text).get("tools", "")
    if re.fullmatch(r"\[?\s*[\"']?\*[\"']?\s*\]?", tools):
        out.append(Finding("agent-all-tools", "medium", file, 1, "the agent is given every tool"))
    return out


# ---- MCP server configurations ------------------------------------------------------------------------------------
_SECRET_NAME = re.compile(r"(?i)(token|secret|passw(or)?d|api[_-]?key|apikey|auth|credential|private[_-]?key|bearer)")
_REFERENCE = re.compile(r"^\s*(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|\{env:[^}]+\}|<[^>]+>|)\s*$")
_BEARER_REF = re.compile(r"^\s*(?:Bearer|Basic|Token)\s+(?:\$\{[^}]+\}|\$[A-Za-z_]\w*|\{env:[^}]+\})\s*$", re.I)
_SHELLS = {"sh", "bash", "zsh", "dash", "fish", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"}
_RUNNERS = {"npx", "bunx", "pnpx", "uvx", "pipx", "yarn", "pnpm"}


def _servers(doc: Any) -> dict[str, dict]:
    """The servers in a configuration: ``mcpServers`` (Claude, Cursor, Copilot agent), ``servers`` (VS Code) or
    ``mcp_servers`` (Codex)."""
    if not isinstance(doc, dict):
        return {}
    for key in ("mcpServers", "servers", "mcp_servers", "mcp"):
        v = doc.get(key)
        if isinstance(v, dict):
            if key == "mcp" and isinstance(v.get("servers"), dict):
                v = v["servers"]
            return {str(k): s for k, s in v.items() if isinstance(s, dict)}
    return {}


def _unpinned(command: str, args: list[str]) -> Optional[str]:
    prog = os.path.basename(command).lower()
    if prog in _RUNNERS or (prog in ("npm", "pnpm", "yarn") and args[:1] in (["exec"], ["dlx"])):
        pkgs = [a for a in args if a and not a.startswith("-") and a not in ("exec", "dlx", "run", "tool")]
        if not pkgs:
            return None
        pkg = pkgs[0]
        if prog in ("uvx", "pipx"):
            return None if re.search(r"(==|@)\d", pkg) else pkg
        name, _, version = pkg.rpartition("@") if pkg.count("@") > (1 if pkg.startswith("@") else 0) else (pkg, "", "")
        return None if version and version not in ("latest", "next", "*") and re.match(r"\d", version) else pkg
    if prog in ("docker", "podman") and "run" in args:
        rest = args[args.index("run") + 1:]
        image = next((a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or not rest[i - 1].startswith("-")
                      or rest[i - 1] in ("--rm", "-i", "-t", "-it", "--init"))), None)
        if image and "@sha256:" not in image and (":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest")):
            return image
    return None


def scan_mcp_config(doc: Any, file: str, text: str = "") -> list[Finding]:
    out: list[Finding] = []

    def line_of(needle: str) -> int:
        i = text.find(needle) if text and needle else -1
        return _line_of(text, i) if i >= 0 else 1

    for name, s in _servers(doc).items():
        where = f"server {name}"
        url = s.get("url") or s.get("serverUrl") or s.get("httpUrl") or ""
        if isinstance(url, str) and url.lower().startswith("http://") and not _local(url):
            out.append(Finding("mcp-plain-http", "high", file, line_of(url), f"{where} is reached over plain http"))
        command = str(s.get("command") or "")
        args = [str(a) for a in (s.get("args") or []) if isinstance(a, (str, int, float))]
        if command:
            prog = os.path.basename(command).lower()
            if prog in _SHELLS and any(a.lower() in ("-c", "/c", "-command", "-encodedcommand") for a in args):
                out.append(Finding("mcp-shell", "high", file, line_of(command), f"{where} starts through {prog}"))
            joined = " ".join([command, *args])
            if _REMOTE_SCRIPT.search(joined):
                out.append(Finding("remote-script", "critical", file, line_of(command), f"{where} pipes a download into a shell"))
            pkg = _unpinned(command, args)
            if pkg:
                out.append(Finding("mcp-unpinned", "medium", file, line_of(pkg), f"{where} runs {pkg} without a pinned version"))
        for section in ("env", "headers", "requestInit"):
            values = s.get(section)
            if section == "requestInit" and isinstance(values, dict):
                values = values.get("headers")
            if not isinstance(values, dict):
                continue
            for k, v in values.items():
                if not isinstance(v, str) or not _SECRET_NAME.search(str(k)):
                    continue
                if _REFERENCE.match(v) or _BEARER_REF.match(v):
                    continue
                out.append(Finding("mcp-inline-credential", "critical", file, line_of(f"\"{k}\""),
                                   f"{where}'s {section} {k} holds a value, not a reference"))
    return out


# ---- tool settings (Claude Code) ----------------------------------------------------------------------------------
_BROAD = re.compile(r"^(?:\*|Bash|Bash\(\*\)|Bash\(\*:\*\)|Bash\(:\*\)|Shell|Shell\(\*\)|run_terminal_cmd|computer)$")
_DESTRUCTIVE = re.compile(r"\b(?:rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|git\s+push\s+(?:-f|--force)|git\s+reset\s+--hard|"
                          r"mkfs|dd\s+if=|chmod\s+-R\s+777|terraform\s+(?:apply|destroy)\s+-auto-approve|"
                          r"kubectl\s+delete|drop\s+(?:table|database))", re.I)
_NETWORK = re.compile(r"\b(?:curl|wget|nc|ncat|netcat|scp|rsync|ssh|Invoke-WebRequest|iwr)\b|https?://", re.I)


def scan_settings(doc: Any, file: str, text: str = "") -> list[Finding]:
    out: list[Finding] = []
    if not isinstance(doc, dict):
        return out

    def line_of(needle: str) -> int:
        i = text.find(needle) if text and needle else -1
        return _line_of(text, i) if i >= 0 else 1

    perms = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
    for entry in perms.get("allow") or []:
        e = str(entry).strip()
        if _BROAD.match(e):
            out.append(Finding("permissions-broad", "high", file, line_of(e), f"\"{e}\" lets the agent run any command"))
        elif _DESTRUCTIVE.search(e):
            out.append(Finding("permissions-destructive", "medium", file, line_of(e), f"\"{e}\" runs without asking"))
    mode = perms.get("defaultMode") or doc.get("defaultMode")
    if mode in ("bypassPermissions", "dontAsk"):
        out.append(Finding("permissions-broad", "high", file, line_of(str(mode)), f"defaultMode {mode} switches prompts off"))
    if doc.get("dangerouslySkipPermissions") or doc.get("skipDangerousModePermissionPrompt"):
        out.append(Finding("permissions-broad", "high", file, 1, "permission prompts are switched off"))
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                cmd = str((h or {}).get("command") or "") if isinstance(h, dict) else ""
                if not cmd:
                    continue
                n = line_of(cmd[:40])
                if _REMOTE_SCRIPT.search(cmd):
                    out.append(Finding("remote-script", "critical", file, n, f"the {event} hook pipes a download into a shell"))
                elif _NETWORK.search(cmd):
                    out.append(Finding("hook-network", "high", file, n, f"the {event} hook reaches the network"))
                else:
                    out.append(Finding("hook", "info", file, n, f"the {event} hook runs: {cmd[:80]}"))
    return out


def _toml(text: str) -> dict:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ has it
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


# ---- walking a folder ---------------------------------------------------------------------------------------------
def _match(rel: str, patterns: Iterable[str]) -> bool:
    """``rel`` (or its tail below any folder: a package's own AGENTS.md or .mcp.json in a monorepo) matches."""
    parts = rel.split("/")
    return any(_match_one("/".join(parts[i:]), patterns) for i in range(len(parts)))


def _match_one(rel: str, patterns: Iterable[str]) -> bool:
    for p in patterns:
        if "**" in p:
            head, tail = p.split("**", 1)
            tail = tail.lstrip("/")
            if rel.startswith(head) and fnmatch.fnmatch(rel.rsplit("/", 1)[-1], tail.rsplit("/", 1)[-1]):
                return True
        elif fnmatch.fnmatch(rel, p) and rel.count("/") == p.count("/"):
            return True
    return False


def files(root: str) -> list[str]:
    """The agent-setup files under ``root`` (relative paths), never following a link out of it."""
    root = os.path.realpath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full) or not (_match(rel, MCP_FILES + SETTINGS_FILES + CODEX_FILES + TEXT_FILES)):
                continue
            out.append(rel)
    return out


def scan_file(root: str, rel: str) -> list[Finding]:
    path = os.path.join(root, rel)
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [Finding("not-read", "info", rel, 1, "larger than 1 MB: not read", "check it by hand")]
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return [Finding("not-read", "info", rel, 1, f"could not be read ({e.strerror})", "check it by hand")]
    if _match(rel, MCP_FILES):
        try:
            doc = json.loads(_strip_jsonc(text))
        except ValueError:
            return _secrets(text, rel) + [Finding("not-read", "info", rel, 1, "not valid JSON: only checked for secrets",
                                                         "fix the JSON so its servers can be checked")]
        return _secrets(text, rel) + scan_mcp_config(doc, rel, text)
    if _match(rel, SETTINGS_FILES):
        try:
            doc = json.loads(_strip_jsonc(text))
        except ValueError:
            doc = {}
        return _secrets(text, rel) + scan_settings(doc, rel, text) + scan_mcp_config(doc, rel, text)
    if _match(rel, CODEX_FILES):
        return _secrets(text, rel) + scan_mcp_config(_toml(text), rel, text)
    if _match(rel, AGENT_FILES):
        return scan_agent_definition(text, rel)
    return scan_text(text, rel)


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (VS Code's files) as JSON; strings are left alone."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def scan_path(root: str) -> list[Finding]:
    root = os.path.realpath(root)
    if os.path.isfile(root):
        return scan_file(os.path.dirname(root), os.path.basename(root))
    found: list[Finding] = []
    for rel in files(root):
        found += scan_file(root, rel)
    return sorted(found, key=lambda f: (-SEVERITIES.index(f.severity), f.file, f.line, f.rule))


# ---- reports ------------------------------------------------------------------------------------------------------
def to_sarif(findings: list[Finding]) -> dict:
    level = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
    rules = sorted({f.rule for f in findings})
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cairn agents scan", "informationUri": "https://cairn.dev/docs/agents-scan",
                                "rules": [{"id": r, "shortDescription": {"text": r.replace("-", " ")},
                                           "help": {"text": FIX.get(r, "")}} for r in rules]}},
            "results": [{"ruleId": f.rule, "level": level[f.severity],
                         "message": {"text": f"{f.message}. Fix: {f.fix}" if f.fix else f.message},
                         "properties": {"severity": f.severity},
                         "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.file},
                                                             "region": {"startLine": max(1, f.line)}}}]}
                        for f in findings],
        }],
    }


def to_text(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    lines = [f"{f.severity.upper():8} {f.file}:{f.line}  {f.rule}: {f.message}\n         fix: {f.fix}" for f in findings]
    counts = {s: sum(1 for f in findings if f.severity == s) for s in reversed(SEVERITIES)}
    lines.append(", ".join(f"{n} {s}" for s, n in counts.items() if n))
    return "\n".join(lines)


def to_json(findings: list[Finding]) -> str:
    return json.dumps([asdict(f) for f in findings], indent=2)


# ---- what is registered in Cairn ----------------------------------------------------------------------------------
def fail_on() -> str:
    level = os.environ.get("CAIRN_AGENT_SCAN_FAIL_ON", "high").strip().lower()
    return level if level in SEVERITIES else "high"


def check_agent(name: str, instructions: str) -> list[Finding]:
    """A custom agent's instructions, as the scanner reads an agent file."""
    return scan_text(instructions, f"agent {name}")


def check_mcp_server(name: str, spec: dict) -> list[Finding]:
    """A registered MCP server: its address and any literal credential in it (its sign-in comes from Cairn's
    secrets, never from the definition)."""
    out = scan_mcp_config({"mcpServers": {name: {"url": spec.get("url") or "", "headers": spec.get("headers") or {}}}},
                          f"mcp server {name}")
    for k in ("url", "description"):
        if isinstance(spec.get(k), str):
            out += scan_text(spec[k], f"mcp server {name}", instructions=(k == "description"))
    return out


def blocking(findings: list[Finding], threshold: Optional[str] = None) -> list[Finding]:
    t = threshold or fail_on()
    return [f for f in findings if at_least(f.severity, t)]
