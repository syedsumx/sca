"""Guard hooks (L164): an AI coding tool asks Cairn before its agent acts, and is refused unless Cairn says it may.

A tool's pre-tool-use hook runs ``cairn agents guard`` with the pending call on stdin (a command to run, a file to
write or read, a URL to fetch, an MCP tool to call). The command asks Cairn (``POST /v1/agent-guard/decide``), as
the person signed in to the CLI, and answers the tool in its own format: **allow**, **ask** (the tool asks the
person, where it can; otherwise refused) or **deny**.

How Cairn decides (:meth:`Guard.decide`):

1. The call is **classified** here, on the server (:func:`classify`), so the rules are the same for everyone and
   change in one place: ``pipe-to-shell``, ``secret-file``, ``destructive-root``, ``guard-tamper`` (refused);
   ``destructive``, ``force-push``, ``push``, ``network``, ``privilege``, ``publish``, ``environment``,
   ``outside-repo``, ``agent-setup``, ``ci-config``, ``plain-http``, ``unparsed`` (the person is asked); anything
   else is allowed. That is the **baseline**.
2. Cairn's **policy decision point** is asked with the person as the actor, a privilege per kind of call
   (``AGENT_RUN_COMMAND``, ``AGENT_WRITE_FILE``, ``AGENT_READ_FILE``, ``AGENT_FETCH_URL``, ``AGENT_CALL_TOOL``)
   and the call's attributes (``resource.classes``, ``resource.agent_client``, ``resource.agent_tool``,
   ``resource.host``, ``resource.mcp_server``, ``resource.repo``), so an operator's policies apply.
3. A policy's DENY always refuses. A policy's ALLOW lifts a baseline *ask* to allow (an operator's deliberate
   grant), never a baseline refusal. Where no policy matches, the baseline stands.

This is a guardrail for an agent's mistakes and for instructions planted in what it reads, not a sandbox: the
classes are read from the command's text, and a determined person on their own machine can run anything.

**Fail closed.** Anything short of a clear answer is a refusal: Cairn unreachable or slow, no sign-in, an error
from the decision point, an answer the command cannot read, a hook input it cannot read. The hook's command line
ends in ``|| exit 2`` (see :func:`hook_command`), so a missing or crashing ``cairn`` blocks the call too.

What is recorded: refusals and asks are audited (``agent_guard.refused``, ``agent_guard.asked``) with the kind,
the classes, the tool and a hash of the command or path, never the text (a command line can hold a credential).
The text is used to classify and is not stored or logged.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

KINDS = ("command", "write", "read", "fetch", "tool")
PRIVILEGE = {"command": "AGENT_RUN_COMMAND", "write": "AGENT_WRITE_FILE", "read": "AGENT_READ_FILE",
             "fetch": "AGENT_FETCH_URL", "tool": "AGENT_CALL_TOOL"}
CLIENTS = ("claude-code", "cursor", "copilot")
SERVER = "cairn"

# The baseline: what each class of call gets when no policy speaks.
DENY = {"pipe-to-shell": "a download piped into a shell runs code nobody has read",
        "secret-file": "it touches a file that holds credentials",
        "destructive-root": "it deletes a home, root, repository or everything in a folder",
        "guard-tamper": "it changes the guard's own files or the tool's hook settings"}
ASK = {"destructive": "it deletes or overwrites work that may not be recoverable",
       "force-push": "it rewrites history others may have",
       "push": "it publishes commits",
       "network": "it reaches the network",
       "privilege": "it runs with raised privileges or loosens permissions",
       "publish": "it publishes a package, an image or a release",
       "environment": "it prints the environment, which often holds credentials",
       "outside-repo": "it writes outside the repository",
       "agent-setup": "it changes what agents read here (MCP servers, skills, instructions)",
       "ci-config": "it changes what CI runs",
       "plain-http": "it fetches over plain http",
       "unparsed": "the command could not be read reliably"}

# Paths that hold credentials (matched on the path as written, lower-cased, / separators).
SENSITIVE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials|id_(?:rsa|dsa|ecdsa|ed25519)|"
    r"credentials(?:\.json)?|secrets?\.(?:ya?ml|json|toml)|[\w.-]+\.(?:pem|key|p12|pfx|jks|keystore)|shadow|"
    r"gateway-identity\.json|kubeconfig)$"
    r"|(?:^|/)\.(?:ssh|aws|gnupg|azure|kube)/|(?:^|/)\.config/(?:gcloud|cairn|gh)/|(?:^|/)\.docker/config\.json$")
_NOT_SENSITIVE = re.compile(r"(?:^|/)\.env\.(?:example|sample|template|dist)$|\.pub$")
GUARD_PATH = re.compile(r"(?:^|/)(?:\.claude/settings(?:\.local)?\.json|\.cursor/hooks\.json|\.github/hooks/[^/]+|"
                        r"\.cairn/[^/]+)$")
SETUP_PATH = re.compile(r"(?:^|/)(?:\.mcp\.json|\.vscode/mcp\.json|\.cursor/mcp\.json|\.junie/mcp/mcp\.json|"
                        r"\.github/copilot-mcp\.json|\.codex/config\.toml|agents\.md|claude\.md|gemini\.md|"
                        r"\.cursorrules|\.github/copilot-instructions\.md)$"
                        r"|(?:^|/)(?:\.claude/(?:skills|agents|commands)|\.github/(?:agents|skills|prompts)|"
                        r"\.cursor/rules|\.agents/skills)/")
CI_PATH = re.compile(r"(?:^|/)(?:\.github/workflows/|\.gitlab-ci\.yml$|\.circleci/|jenkinsfile$|azure-pipelines\.yml$)")

_FETCHERS = {"curl", "wget", "fetch", "http", "https", "iwr", "invoke-webrequest", "aria2c"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "python", "python3", "node", "perl", "ruby"}
_NETWORK = _FETCHERS | {"nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "socat", "openssl"}
_PRIVILEGE = {"sudo", "su", "doas", "chown", "chgrp", "setcap", "mount", "umount"}
_ROOTS = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", ".", "./", "./*", "..", "../", "*",
          ".git", "./.git", ".git/"}
_SPLIT = {";", "&&", "||", "|", "|&", "&", "\n"}


def sensitive(path: str) -> bool:
    p = (path or "").replace("\\", "/").lower()
    return bool(SENSITIVE_PATH.search(p)) and not _NOT_SENSITIVE.search(p)


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lower()


def _segments(command: str) -> Optional[list[list[str]]]:
    """The command's simple commands as word lists (split at ; && || | &), or None when it cannot be read."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        lex.commenters = ""
        words = list(lex)
    except ValueError:
        return None
    out: list[list[str]] = [[]]
    for w in words:
        if w in _SPLIT or (w and set(w) <= set(";&|")):
            out.append([])
        else:
            out[-1].append(w)
    return [s for s in out if s]


def _name(word: str) -> str:
    return word.rsplit("/", 1)[-1].lower()


def _strip_prefix(seg: list[str]) -> list[str]:
    """Without leading ``VAR=value`` words and wrappers such as ``env``, ``time``, ``nohup``, ``xargs``."""
    i = 0
    while i < len(seg) and (re.match(r"^[A-Za-z_]\w*=", seg[i]) or _name(seg[i]) in {"env", "time", "nohup", "command",
                                                                                   "exec", "xargs", "nice", "timeout"}):
        i += 1
        while i < len(seg) and re.match(r"^-|^\d+[smh]?$", seg[i]) and _name(seg[i - 1]) in {"timeout", "nice", "xargs"}:
            i += 1
    return seg[i:]


def classify_command(command: str) -> set[str]:
    """The classes a shell command falls in (see the module's list)."""
    classes: set[str] = set()
    text = command or ""
    low = text.lower()
    # A download run as code: piped into an interpreter, or substituted into one.
    if re.search(r"\b(?:curl|wget|fetch|iwr|invoke-webrequest)\b[^\n;&]*\|\s*(?:sudo\s+)?(?:env\s+)?"
                 r"(?:ba|z|da|k)?sh\b|\b(?:curl|wget)\b[^\n;&]*\|\s*(?:sudo\s+)?(?:python3?|node|perl|ruby|pwsh)\b"
                 r"|\b(?:ba|z)?sh\b[^\n;&|]*<\(\s*(?:curl|wget)\b|\b(?:ba|z)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b"
                 r"|\biex\b[^\n]*\b(?:iwr|invoke-webrequest|downloadstring)\b|\beval\b[^\n]*\$\(\s*(?:curl|wget)\b", low):
        classes.add("pipe-to-shell")
    if re.search(r"[`]|\$\(", text) or "<<" in text:
        nested = True                                    # substitutions and here-documents hide what runs
    else:
        nested = False
    segs = _segments(text)
    if segs is None:
        classes.add("unparsed")
        segs = [re.split(r"\s+", part.strip()) for part in re.split(r"[;&|\n]+", text) if part.strip()]
    for raw in segs:
        seg = _strip_prefix(raw)
        if not seg:
            if any(_name(w) == "env" for w in raw):       # a bare env prints the environment
                classes.add("environment")
            continue
        if _name(seg[0]) in _PRIVILEGE:
            classes.add("privilege")
            if _name(seg[0]) in {"sudo", "doas"}:           # what it runs is judged too
                rest = seg[1:]
                while rest and rest[0].startswith("-"):
                    rest = rest[2:] if rest[0] in {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T"} else rest[1:]
                seg = _strip_prefix(rest) or seg
        cmd, args = _name(seg[0]), seg[1:]
        flags = "".join(a[1:] for a in args if re.match(r"^-[A-Za-z]+$", a)).lower()
        longs = {a.split("=", 1)[0].lower() for a in args if a.startswith("--")}
        plain = [a for a in args if not a.startswith("-")]
        if any(sensitive(a) or sensitive(a.split("=", 1)[-1]) or sensitive(a.lstrip("@<>")) for a in seg):
            classes.add("secret-file")
        if any(GUARD_PATH.search(_norm(a.lstrip("@<>"))) for a in seg) and cmd not in {"cat", "ls", "grep", "rg", "head",
                                                                                      "tail", "less", "wc", "stat", "diff"}:
            classes.add("guard-tamper")
        if cmd in {"env", "printenv", "set", "export"} and not plain:
            classes.add("environment")
        if cmd in _NETWORK:
            classes.add("network")
        if cmd in {"chmod"} and any(re.fullmatch(r"0?[0-7]?[0-7]?7|a\+[rwx]+|o\+w", a) for a in args):
            classes.add("privilege")
        if cmd == "rm":
            recursive = "r" in flags or "--recursive" in longs
            if recursive and any(p in _ROOTS or p.rstrip("/") in {"/home", "/etc", "/usr", "/var", "/root"} for p in plain):
                classes.add("destructive-root")
            elif recursive or "f" in flags or "--force" in longs:
                classes.add("destructive")
        if cmd in {"dd", "mkfs", "shred", "truncate", "fdisk", "wipefs"} or cmd.startswith("mkfs."):
            classes.add("destructive")
        if cmd == "find" and ("-delete" in args or "-exec" in args and "rm" in args):
            classes.add("destructive")
        if cmd == "git":
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub == "push":
                forced = "f" in flags or longs & {"--force", "--force-with-lease", "--mirror", "--delete"} or \
                    any(p.startswith("+") or p.startswith(":") for p in plain[1:])
                classes.add("force-push" if forced else "push")
            if (sub == "reset" and "--hard" in longs) or (sub == "clean" and "f" in flags) or \
                    (sub in {"checkout", "restore"} and ("." in plain or "--" in args and sub == "checkout")) or \
                    (sub == "branch" and "D" in "".join(a[1:] for a in args if re.match(r"^-[A-Za-z]+$", a))) or \
                    sub in {"filter-branch", "filter-repo"} or (sub == "stash" and {"drop", "clear"} & set(plain)):
                classes.add("destructive")
            if sub in {"clone", "fetch", "pull", "ls-remote", "submodule"}:
                classes.add("network")
        if cmd in {"kubectl", "oc"} and {"delete", "drain"} & set(plain) or cmd == "helm" and {"uninstall", "delete"} & set(plain):
            classes.add("destructive")
        if cmd in {"terraform", "tofu", "pulumi"} and ({"destroy"} & set(plain) or "-destroy" in args):
            classes.add("destructive")
        if cmd == "docker" and ({"prune"} & set(plain) or plain[:1] == ["rm"] or plain[:2] == ["volume", "rm"]):
            classes.add("destructive")
        if (cmd == "docker" and plain[:1] == ["push"]) or (cmd in {"npm", "pnpm", "yarn"} and plain[:1] == ["publish"]) or \
                (cmd == "twine" and plain[:1] == ["upload"]) or (cmd == "cargo" and plain[:1] == ["publish"]) or \
                (cmd == "gh" and plain[:2] in (["release", "create"], ["release", "upload"])):
            classes.add("publish")
        if cmd in _SHELLS and ("-c" in args or "-e" in args) and nested:
            classes.add("unparsed")
    if re.search(r"\b(?:drop\s+(?:table|database|schema)|truncate\s+table|delete\s+from\s+\w+\s*(?:;|$|\"|'))", low):
        classes.add("destructive")
    if re.search(r">\s*/dev/(?:sd|nvme|disk)", low):
        classes.add("destructive")
    if nested and not classes & {"pipe-to-shell"} and re.search(r"\$\(|`", text) and \
            re.search(r"\b(?:curl|wget)\b", low):
        classes.add("network")
    return classes


def classify(kind: str, *, command: str = "", path: str = "", inside_repo: bool = True, url: str = "",
             server: str = "", tool: str = "") -> set[str]:
    """The classes of one pending call."""
    if kind == "command":
        return classify_command(command)
    classes: set[str] = set()
    if kind in ("write", "read"):
        p = _norm(path)
        if sensitive(p):
            classes.add("secret-file")
        if kind == "write":
            if GUARD_PATH.search(p):
                classes.add("guard-tamper")
            elif SETUP_PATH.search(p):
                classes.add("agent-setup")
            if CI_PATH.search(p):
                classes.add("ci-config")
            if not inside_repo:
                classes.add("outside-repo")
    elif kind == "fetch":
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            classes.add("unparsed")
        elif parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            classes.add("plain-http")
        if parts.username or parts.password:
            classes.add("secret-file")                   # a credential in the address
    elif kind == "tool":
        if server and server != SERVER:
            classes.add("external-tool")
    return classes


def baseline(classes: set[str]) -> tuple[str, str]:
    """(decision, why) from the classes alone."""
    for c in sorted(classes):
        if c in DENY:
            return "deny", DENY[c]
    for c in sorted(classes):
        if c in ASK:
            return "ask", ASK[c]
    return "allow", "nothing in it needs a person's say-so"


@dataclass
class Verdict:
    decision: str                                        # allow | ask | deny
    reason: str
    classes: list[str] = field(default_factory=list)
    policy_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason, "classes": self.classes, "policy_ids": self.policy_ids}


class GuardIn(BaseModel):
    kind: str = Field(pattern="^(command|write|read|fetch|tool)$")
    client: str = Field(default="", max_length=40)
    tool: str = Field(default="", max_length=200)        # the tool's own name for the call (Bash, Write, ...)
    command: str = Field(default="", max_length=100_000)
    path: str = Field(default="", max_length=4_096)       # relative to the repository when inside it
    inside_repo: bool = True
    url: str = Field(default="", max_length=8_192)
    server: str = Field(default="", max_length=200)       # an MCP server's name
    repo: str = Field(default="", max_length=200)         # the repository folder's name


class Guard:
    def __init__(self, pdp, audit=None):
        self.pdp, self.audit = pdp, audit

    def decide(self, actor: str, req: GuardIn, agent: Optional[str] = None) -> Verdict:
        from cairn.governance.pdp import AuthorizeRequest, ResourceRef

        classes = classify(req.kind, command=req.command, path=req.path, inside_repo=req.inside_repo, url=req.url,
                           server=req.server, tool=req.tool)
        base, why = baseline(classes)
        host = (urlsplit(req.url).hostname or "").lower() if req.kind == "fetch" else ""
        attrs: dict[str, Any] = {"classes": sorted(classes), "agent_client": req.client, "agent_tool": req.tool,
                                 "agent_kind": req.kind, "host": host, "mcp_server": req.server, "repo": req.repo,
                                 "inside_repo": req.inside_repo}
        try:
            got = self.pdp.decide(AuthorizeRequest(
                actor=actor, privilege=PRIVILEGE[req.kind],
                resource=ResourceRef(type="agentAction", urn=f"urn:cairn:agentAction:{req.kind}"),
                resource_attributes=attrs, environment={"channel": "agent-guard"}))
            policy, ids = got.decision, list(got.policy_ids)
        except Exception as e:                           # noqa: BLE001 - no clear answer is a refusal
            verdict = Verdict("deny", f"Cairn's policy decision failed ({type(e).__name__}): refused", sorted(classes))
            self._record(actor, agent, req, verdict)
            return verdict
        if policy == "DENY" and ids:                     # a policy that matched; "no policy allows" is no opinion
            verdict = Verdict("deny", "a Cairn policy refuses it", sorted(classes), ids)
        elif base == "deny":
            verdict = Verdict("deny", why, sorted(classes))
        elif base == "ask" and policy == "ALLOW":
            verdict = Verdict("allow", "a Cairn policy allows it", sorted(classes), ids)
        else:
            verdict = Verdict(base, why, sorted(classes), ids if policy == "ALLOW" else [])
        self._record(actor, agent, req, verdict)
        return verdict

    def _record(self, actor: str, agent: Optional[str], req: GuardIn, verdict: Verdict) -> None:
        if self.audit is None or verdict.decision == "allow":
            return
        what = req.command or req.path or req.url or f"{req.server}:{req.tool}"
        self.audit.record("agent_guard", "agent_guard.refused" if verdict.decision == "deny" else "agent_guard.asked",
                          actor, None, {"kind": req.kind, "classes": verdict.classes, "client": req.client,
                                        "tool": req.tool, "repo": req.repo, "policies": verdict.policy_ids,
                                        "agent": agent, "sha256": hashlib.sha256(what.encode()).hexdigest()})


def build_router(guard: Guard, current_principal) -> Any:
    from fastapi import APIRouter, Depends

    router = APIRouter(prefix="/v1/agent-guard", tags=["agents"])

    @router.post("/decide")
    def decide(body: GuardIn, p=Depends(current_principal)):
        return guard.decide(p.urn, body, agent=getattr(p, "agent_id", None)).as_dict()

    return router


# ---- the hook side: each tool's input read, each tool's answer written -------------------------------------------
TIMEOUT_S = 8                                            # the command's own limit on asking Cairn
HOOK_TIMEOUT_S = 20                                      # the tool's limit on the hook: longer, so the command answers


def hook_command(client: str) -> str:
    """The command a tool's hook runs. ``|| exit 2`` makes a missing or crashing ``cairn`` block the call (exit 2 is
    "blocked" in every tool; other failures are not)."""
    return f"cairn agents guard --client {client} || exit 2"


def _ours(command: Any) -> bool:
    return isinstance(command, str) and "cairn agents guard" in command


def _json_object(existing: Optional[str], what: str) -> dict:
    if not existing or not existing.strip():
        return {}
    try:
        doc = json.loads(existing)
    except ValueError:
        raise ValueError(f"{what} is not plain JSON (comments?): add the hook by hand, or move it aside")
    if not isinstance(doc, dict):
        raise ValueError(f"{what} is not a JSON object")
    return doc


def _list_at(doc: dict, *keys: str) -> list:
    node: Any = doc
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            raise ValueError(f"its {k} is not an object")
    got = node.setdefault(keys[-1], [])
    if not isinstance(got, list):
        raise ValueError(f"its {keys[-1]} is not a list")
    return got


CLAUDE_MATCHER = "^(Bash|Write|Edit|MultiEdit|NotebookEdit|Read|WebFetch|mcp__(?!cairn__).*)$"
HOOK_FILES = {"claude-code": ".claude/settings.json", "cursor": ".cursor/hooks.json",
              "copilot": ".github/hooks/cairn-guard.json"}


def install(client: str, existing: Optional[str]) -> str:
    """The tool's hook file with Cairn's guard in it (other hooks and settings kept; Cairn's own entry replaced)."""
    doc = _json_object(existing, HOOK_FILES[client])
    cmd = hook_command(client)
    if client == "claude-code":
        entries = _list_at(doc, "hooks", "PreToolUse")
        entries[:] = [e for e in entries if not (isinstance(e, dict) and any(
            isinstance(h, dict) and _ours(h.get("command")) for h in e.get("hooks") or []))]
        entries.append({"matcher": CLAUDE_MATCHER, "hooks": [
            {"type": "command", "command": cmd, "timeout": HOOK_TIMEOUT_S, "onFailure": "block"}]})
    elif client == "cursor":
        doc.setdefault("version", 1)
        for event in ("beforeShellExecution", "beforeMCPExecution", "beforeReadFile"):
            entries = _list_at(doc, "hooks", event)
            entries[:] = [e for e in entries if not (isinstance(e, dict) and _ours(e.get("command")))]
            entries.append({"command": cmd, "failClosed": True, "timeout": HOOK_TIMEOUT_S})
    elif client == "copilot":
        doc.setdefault("version", 1)
        entries = _list_at(doc, "hooks", "preToolUse")
        entries[:] = [e for e in entries if not (isinstance(e, dict) and _ours(e.get("bash")))]
        entries.append({"type": "command", "bash": cmd, "timeoutSec": HOOK_TIMEOUT_S})
    else:
        raise ValueError(f"no guard hook for {client}")
    return json.dumps(doc, indent=2) + "\n"


def _relative(path: str, cwd: str) -> tuple[str, bool]:
    """(the path relative to the repository when inside it, whether it is inside)."""
    import os

    if not path:
        return "", True
    full = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path)))
    root = os.path.realpath(cwd or os.getcwd())
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:
        inside = False
    return (os.path.relpath(full, root).replace(os.sep, "/") if inside else full.replace(os.sep, "/")), inside


def request_from(client: str, event: dict) -> Optional[dict]:
    """The decide request for a tool's hook event, or None when the call needs no decision (an ordinary read, a
    search, a call to Cairn's own MCP server, which Cairn governs itself). Raises ValueError for an event it cannot
    read: the caller refuses."""
    import os

    if not isinstance(event, dict):
        raise ValueError("the hook input is not an object")
    roots = event.get("workspace_roots")
    cwd = event.get("cwd") or (roots[0] if isinstance(roots, list) and roots else None) or os.getcwd()
    if not isinstance(cwd, str):
        raise ValueError("the hook input's folder is not text")
    base = {"client": client, "repo": os.path.basename(cwd.rstrip("/\\")) or ""}
    if client in ("claude-code", "copilot"):
        name = event.get("tool_name") or event.get("toolName")
        args = event.get("tool_input") if "tool_input" in event else event.get("toolArgs")
        if isinstance(args, str):                        # Copilot sends the arguments as a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                raise ValueError("the tool's arguments are not JSON")
        if not isinstance(name, str) or not name or not isinstance(args, dict):
            raise ValueError("the hook input has no tool name or arguments")
        return _from_tool(base, name, args, cwd)
    if client == "cursor":
        hook = event.get("hook_event_name")
        if hook == "beforeShellExecution":
            if not isinstance(event.get("command"), str):
                raise ValueError("the hook input has no command")
            return {**base, "kind": "command", "tool": "shell", "command": event["command"]}
        if hook == "beforeMCPExecution":
            return _from_mcp(base, str(event.get("server") or event.get("server_name") or ""),
                             str(event.get("tool_name") or ""), event.get("command"))
        if hook == "beforeReadFile":
            path = event.get("file_path")
            if not isinstance(path, str):
                raise ValueError("the hook input has no file path")
            rel, inside = _relative(path, cwd)
            return {**base, "kind": "read", "tool": "read", "path": rel, "inside_repo": inside} if sensitive(rel) else None
        raise ValueError(f"an unknown hook event {hook!r}")
    raise ValueError(f"an unknown client {client!r}")


def _from_mcp(base: dict, server: str, tool: str, command: Any = None) -> Optional[dict]:
    if server == SERVER:
        return None
    if not server and not tool:
        raise ValueError("the hook input names no MCP server or tool")
    return {**base, "kind": "tool", "tool": tool, "server": server or "unknown"}


def _from_tool(base: dict, name: str, args: dict, cwd: str) -> Optional[dict]:
    low = name.lower()
    if low.startswith("mcp__"):
        _, server, *rest = name.split("__")
        return _from_mcp(base, server, "__".join(rest))
    if low in ("bash", "shell", "run_in_terminal", "runinterminal", "execute", "powershell"):
        command = args.get("command")
        if not isinstance(command, str):
            raise ValueError("the hook input has no command")
        return {**base, "kind": "command", "tool": name, "command": command}
    if low in ("write", "edit", "multiedit", "notebookedit", "create", "str_replace", "str_replace_editor",
               "create_file", "edit_file", "insert_edit_into_file", "apply_patch"):
        path = args.get("file_path") or args.get("path") or args.get("notebook_path") or args.get("filePath")
        if not isinstance(path, str) or not path:
            raise ValueError("the hook input has no file path")
        rel, inside = _relative(path, cwd)
        return {**base, "kind": "write", "tool": name, "path": rel, "inside_repo": inside}
    if low in ("read", "view", "read_file", "notebookread"):
        path = args.get("file_path") or args.get("path") or args.get("filePath")
        if not isinstance(path, str) or not path:
            raise ValueError("the hook input has no file path")
        rel, inside = _relative(path, cwd)
        return {**base, "kind": "read", "tool": name, "path": rel, "inside_repo": inside} if sensitive(rel) else None
    if low in ("webfetch", "fetch", "web_fetch", "fetch_webpage"):
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("the hook input has no URL")
        return {**base, "kind": "fetch", "tool": name, "url": url}
    if low in ("grep", "glob", "ls", "search", "websearch", "todowrite", "task", "agent", "skill", "toolsearch",
               "askuserquestion", "exitplanmode", "taskcreate", "taskupdate", "tasklist", "bashoutput", "killshell"):
        return None
    # A tool this guard does not know: Cairn is asked about it as a tool call, so a policy can speak.
    return {**base, "kind": "tool", "tool": name, "server": "builtin"}


def answer(client: str, decision: str, reason: str) -> tuple[str, str, int]:
    """(stdout, stderr, exit code) telling the tool what Cairn decided.

    * ``allow``: nothing printed, exit 0. The tool goes on with its own permission rules; the guard never grants
      past them.
    * ``ask``: the tool's own "ask the person" answer, where it has one (Claude Code, Cursor); refused elsewhere.
    * ``deny``: the reason on stderr and exit 2, which blocks the call in every tool.
    """
    if client not in CLIENTS:
        raise ValueError(f"an unknown client {client!r}")
    reason = f"Cairn guard: {reason}"
    if decision == "allow":
        return "", "", 0
    if decision == "ask" and client == "claude-code":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                  "permissionDecisionReason": reason}}), "", 0
    if decision == "ask" and client == "cursor":
        return json.dumps({"permission": "ask", "user_message": reason, "agent_message": reason}), "", 0
    if decision == "ask":
        reason += " (this tool cannot ask you, so it is refused: run it yourself if it is right)"
    return "", reason, 2


def run_hook(client: str, raw: str, ask) -> tuple[str, str, int]:
    """One hook call: ``raw`` is the tool's input, ``ask(request) -> dict`` asks Cairn. Anything unclear refuses."""
    try:
        if client not in CLIENTS:
            raise ValueError(f"an unknown client {client!r}")
        req = request_from(client, json.loads(raw))
    except (ValueError, TypeError, AttributeError, IndexError, KeyError) as e:
        return "", f"Cairn guard: the hook input could not be read ({e}): refused", 2
    if req is None:
        return "", "", 0
    try:
        doc = ask(req)
    except Exception as e:                               # noqa: BLE001 - no answer is a refusal
        return "", f"Cairn guard: Cairn could not be asked ({e}): refused", 2
    decision = doc.get("decision") if isinstance(doc, dict) else None
    if decision not in ("allow", "ask", "deny"):
        return "", "Cairn guard: Cairn's answer could not be read: refused", 2
    return answer(client, decision, str(doc.get("reason") or decision))


__all__ = ["ASK", "CLIENTS", "DENY", "Guard", "GuardIn", "KINDS", "PRIVILEGE", "Verdict", "answer", "baseline",
           "build_router", "classify", "classify_command", "hook_command", "install", "request_from", "run_hook",
           "sensitive"]
