"""Shell completion: ``cairn completion bash|zsh|fish|powershell`` prints a script that asks ``cairn __complete`` for
the candidates, so commands, options, choices, profile names and flow names are always the ones this ``cairn`` knows
(the flows from Cairn, as you, within two seconds; nothing when it cannot be reached).

``cairn __complete c:<current word> <earlier words…>`` prints one candidate per line. It is not listed in the help.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

SHELLS = ("bash", "zsh", "fish", "powershell")

BASH = r"""# cairn completion for bash: add to ~/.bashrc
#   source <(cairn completion bash)
_cairn_complete() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local IFS=$'\n'
    COMPREPLY=( $(compgen -W "$(cairn __complete "c:${cur}" "${COMP_WORDS[@]:1:COMP_CWORD-1}" 2>/dev/null)" -- "${cur}") )
}
complete -o default -F _cairn_complete cairn
"""

ZSH = r"""#compdef cairn
# cairn completion for zsh: add to ~/.zshrc, after compinit
#   source <(cairn completion zsh)
_cairn() {
    local -a candidates
    candidates=("${(@f)$(cairn __complete "c:${words[CURRENT]}" "${(@)words[2,CURRENT-1]}" 2>/dev/null)}")
    if (( ${#candidates[@]} )) && [[ -n "${candidates[1]}" ]]; then
        compadd -- "${candidates[@]}"
    else
        _files
    fi
}
compdef _cairn cairn
"""

FISH = r"""# cairn completion for fish: save as ~/.config/fish/completions/cairn.fish
#   cairn completion fish > ~/.config/fish/completions/cairn.fish
function __cairn_complete
    set -l tokens (commandline -opc)
    set -e tokens[1]
    cairn __complete "c:"(commandline -ct) $tokens 2>/dev/null
end
complete -c cairn -a '(__cairn_complete)'
"""

POWERSHELL = r"""# cairn completion for PowerShell: add to your $PROFILE
#   cairn completion powershell | Out-String | Invoke-Expression
Register-ArgumentCompleter -Native -CommandName cairn, cairn.exe -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.ToString() })
    if ($wordToComplete -ne '' -and $words.Count -gt 0) {
        $words = if ($words.Count -gt 1) { $words[0..($words.Count - 2)] } else { @() }
    }
    & cairn __complete "c:$wordToComplete" @words 2>$null | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
"""

SCRIPTS = {"bash": BASH, "zsh": ZSH, "fish": FISH, "powershell": POWERSHELL}


def cmd_completion(a) -> int:
    sys.stdout.write(SCRIPTS[a.shell])
    return 0


def _subparsers(parser: argparse.ArgumentParser) -> Optional[argparse._SubParsersAction]:
    return next((x for x in parser._actions if isinstance(x, argparse._SubParsersAction)), None)


def _visible_commands(sub: argparse._SubParsersAction) -> list[str]:
    hidden = {c.dest for c in sub._choices_actions if c.help == argparse.SUPPRESS}
    return [n for n in sub.choices if n not in hidden and not n.startswith("_")]


def _dynamic(kind: str, words: list[str]) -> list[str]:
    try:
        if kind == "profiles":
            from cairn_cli.config import Config

            return sorted(Config.load().profiles)
        if kind == "flows":
            from cairn_cli.session import Session

            profile = next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "--profile"), None)
            s = Session.open(profile, quiet=True)
            try:
                return sorted(f["name"] for f in s.call("GET", "/v1/flows", timeout=2) or [] if f.get("name"))
            finally:
                s.close()
    except Exception:  # noqa: BLE001 - completion never fails loudly
        return []
    return []


def candidates(root: argparse.ArgumentParser, words: list[str], current: str) -> list[str]:
    parser, positionals, expect = root, 0, None
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and w != "-":
            name = w.split("=", 1)[0]
            action = parser._option_string_actions.get(name)
            if action is not None and action.nargs != 0 and "=" not in w:
                if i + 1 == len(words):
                    expect = action
                i += 1
        else:
            sub = _subparsers(parser)
            if sub is not None and w in sub.choices:
                parser, positionals = sub.choices[w], 0
            else:
                positionals += 1
        i += 1
    if expect is not None:
        if getattr(expect, "completer", None):
            return [c for c in _dynamic(expect.completer, words) if c.startswith(current)]
        if expect.choices:
            return [str(c) for c in expect.choices if str(c).startswith(current)]
        return []                                     # a file or free text: the shell's own completion
    if current.startswith("-"):
        opts = sorted({o for act in parser._actions if act.help != argparse.SUPPRESS for o in act.option_strings})
        return [o for o in opts if o.startswith(current)]
    sub = _subparsers(parser)
    if sub is not None:
        return [n for n in _visible_commands(sub) if n.startswith(current)]
    pos = [act for act in parser._actions if not act.option_strings and not isinstance(act, argparse._SubParsersAction)]
    if positionals < len(pos):
        act = pos[positionals]
        if getattr(act, "completer", None):
            return [c for c in _dynamic(act.completer, words) if c.startswith(current)]
        if act.choices:
            return [str(c) for c in act.choices if str(c).startswith(current)]
    return []


def run_complete(root: argparse.ArgumentParser, argv: list[str]) -> int:
    current = argv[0][2:] if argv and argv[0].startswith("c:") else ""
    for c in candidates(root, argv[1:], current):
        print(c)
    return 0
