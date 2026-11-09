"""``cairn``: the command line for people (``cairn-cli``, and the single-file binaries built from it).

The full ``cairn-platform`` install registers the same commands beside the operators' ones (:func:`register` from
:mod:`cairn.cli`); this entry point has only what works over HTTP as the signed-in person, and imports no server code.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from typing import Callable, Optional

from cairn_cli import __version__
from cairn_cli.errors import ERROR, EXIT_CODES, INTERRUPTED, CliError
from cairn_cli.output import FORMATS

EPILOG = "exit codes: " + "; ".join(f"{k} {v.split(':')[0].split(' (')[0]}" for k, v in EXIT_CODES.items())


class CairnParser(argparse.ArgumentParser):
    """argparse, with "did you mean …?" for a mistyped command."""

    def error(self, message: str):
        m = re.search(r"invalid choice: '([^']*)' \(choose from (.*)\)", message)
        if m:
            choices = [c.strip().strip("'\"") for c in m.group(2).split(",")]
            close = difflib.get_close_matches(m.group(1), [c for c in choices if not c.startswith("_")], n=3, cutoff=0.6)
            if close and "did you mean" not in message:
                message = f"invalid choice: '{m.group(1)}'; did you mean {' or '.join(repr(c) for c in close)}?"
        super().error(message)


def add_global_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    """``--profile``, ``--output``, ``--quiet`` and ``--no-color``: before the command (defaults) or after it."""
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    a = p.add_argument("--profile", default=d(None), metavar="NAME",
                       help="the profile (default: CAIRN_PROFILE, else the default profile)")
    a.completer = "profiles"
    p.add_argument("--output", choices=("text", "json"), default=d("text"), help="json: machine-readable output")
    p.add_argument("-q", "--quiet", action="store_true", default=d(False), help="no notes or progress on stderr")
    p.add_argument("--no-color", action="store_true", default=d(False), help="no colour (also: NO_COLOR=1)")


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    add_global_options(p, suppress=True)
    return p


def register(sub, ontology: bool = True, flows: bool = True) -> None:
    """The people's commands, on ``sub`` (the ``cairn`` parser's subparsers)."""
    from cairn_cli import auth, commands, completion

    common = [_common()]

    def add(name: str, fn: Callable, help_: str, **kw) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, parents=common, **kw)
        sp.set_defaults(fn=fn, client_command=True)
        return sp

    lg = add("login", auth.cmd_login, "sign in to a Cairn in your browser (device code), or with --token-stdin")
    lg.add_argument("--url", help="your Cairn, e.g. https://cairn.example.com (kept in the profile)")
    lg.add_argument("--no-browser", action="store_true", help="print the address and code; do not open a browser")
    lg.add_argument("--token-stdin", action="store_true",
                    help="read a token from standard input instead (CI); it is checked, then kept like a sign-in")
    lg.add_argument("--ca-file", help="a CA bundle for a Cairn with a private certificate authority")
    lg.add_argument("--default", action="store_true", help="make this profile the default")
    add("logout", auth.cmd_logout, "sign this device out: its tokens stop working in Cairn and are removed here")
    add("whoami", auth.cmd_whoami, "who you are signed in as: user, groups, profile, Cairn, when the sign-in expires")
    pr = add("profiles", auth.cmd_profiles, "several Cairns: list, use (the default), remove")
    ps = pr.add_subparsers(dest="profiles_cmd", required=True, parser_class=CairnParser)
    ps.add_parser("list", help="the profiles and whether each is signed in", parents=common)
    for verb, what in (("use", "make a profile the default"), ("remove", "sign out and forget a profile")):
        x = ps.add_parser(verb, help=what, parents=common)
        x.add_argument("name").completer = "profiles"

    q = add("query", commands.cmd_query, "run SQL on Trino as you (masks and row filters apply); streams any size",
            description="Runs one read-only statement through Cairn on Trino, as you. Rows stream page by page; "
                        "Ctrl-C cancels the query in Cairn.")
    q.add_argument("sql", nargs="?", help="the SQL (or -f FILE, or standard input)")
    q.add_argument("-f", "--file", help="read the SQL from a file (- for standard input)")
    q.add_argument("--format", choices=FORMATS, help="default: table on a terminal, else csv (json with --output json)")
    q.add_argument("--out", help="write the result to a file instead of standard output")
    q.add_argument("--limit", type=int, help="at most this many rows")
    q.add_argument("--warehouse", help="run on a team's warehouse (needs USE_WAREHOUSE on it)")
    q.add_argument("--connection", help="which Trino connection (default: the one Cairn names)")

    s = add("search", commands.cmd_search, "search the catalog (only what you may see)")
    s.add_argument("text", nargs="+")
    s.add_argument("--type", action="append", choices=("dataset", "dashboard", "chart", "dataJob", "dataFlow",
                                                        "glossaryTerm", "domain", "dataProduct", "mlModel", "container"),
                   help="only this type (repeat for more)")
    s.add_argument("--limit", type=int, default=20)
    sh = add("show", commands.cmd_show, "an asset: description, schema, owners, terms, lineage summary")
    sh.add_argument("asset", help="a urn, or a name such as sales.orders")
    ak = add("ask", commands.cmd_ask, "ask a question of your data: prints the SQL Cairn wrote and the answer")
    ak.add_argument("question", nargs="+")
    ak.add_argument("--dataset", action="append", help="a dataset urn to ask about (repeat; default: Cairn picks)")
    ak.add_argument("--no-run", action="store_true", help="write the SQL only")
    ak.add_argument("--limit", type=int, default=200)

    ic = add("iac", commands.cmd_iac, "everything as code: validate, plan or apply a bundle; export a live Cairn")
    isub = ic.add_subparsers(dest="iac_cmd", required=True, parser_class=CairnParser)
    for verb, what in (("validate", "check a bundle file here, without Cairn"), ("plan", "what applying would change"),
                       ("apply", "make Cairn match the bundle")):
        x = isub.add_parser(verb, help=what, parents=common)
        x.add_argument("file")
        x.add_argument("--var", action="append", help="NAME=value for ${NAME} in the file (else the environment)")
        x.add_argument("--prune", action="store_true", help="delete what this bundle made before and no longer declares")
        x.add_argument("--detailed-exitcode", action="store_true", help="plan exits 2 when there are changes")
        x.add_argument("-v", "--verbose", action="store_true", help="list unchanged resources too")
    x = isub.add_parser("export", help="a live Cairn as code (Terraform with import blocks, or a bundle)", parents=common)
    x.add_argument("--format", choices=["terraform", "yaml"], default="terraform")
    x.add_argument("--kind", action="append", help="only this resource type; repeat for more")
    x.add_argument("--bundle", default="exported", help="the bundle's name (--format yaml)")
    x.add_argument("--out", help="the file to write (default: standard output)")

    if ontology:
        on = add("ontology", commands.cmd_ontology, "Cairn Ontology: its status, or compile it now")
        osub = on.add_subparsers(dest="ontology_cmd", required=True, parser_class=CairnParser)
        osub.add_parser("status", help="the compiler, the last run and what it proposed", parents=common)
        x = osub.add_parser("compile", help="sync the catalog and query log, then compile", parents=common)
        x.add_argument("--mode", choices=["full", "incremental"], default="incremental")
        x.add_argument("--wait", action="store_true", help="wait for the run and import its proposals")
    if flows:
        from cairn.flows.cli import add_parser as flows_parser

        flows_parser(sub)
        sub.choices["flows"].set_defaults(fn=commands.cmd_flows, client_command=True)
    mark_flow_names(sub)

    c = add("completion", completion.cmd_completion, "print a shell completion script (bash, zsh, fish, powershell)")
    c.add_argument("shell", choices=completion.SHELLS)


def mark_flow_names(sub) -> None:
    """Flow names complete from Cairn (``cairn flows run <TAB>``)."""
    fl = sub.choices.get("flows")
    fsub = next((x for x in fl._actions if isinstance(x, argparse._SubParsersAction)), None) if fl else None
    for verb in ("run", "export-airflow"):
        p = fsub.choices.get(verb) if fsub is not None else None
        for act in (p._actions if p is not None else []):
            if act.dest == "name":
                act.completer = "flows"


def build_parser() -> argparse.ArgumentParser:
    p = CairnParser(prog="cairn", description="Cairn from the command line: sign in, then query, search, show and ask "
                    "as yourself.", epilog=EPILOG)
    p.add_argument("--version", action="version", version=f"cairn {__version__}")
    add_global_options(p, suppress=False)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND", parser_class=CairnParser)
    register(sub)
    return p


def dispatch(args) -> int:
    """Runs a people's command: its CliError becomes the message and the exit code."""
    from cairn_cli.output import err_style
    from cairn_cli.session import select

    select(getattr(args, "profile", None), getattr(args, "quiet", False))
    try:
        rc = args.fn(args)
    except CliError as e:
        print(f"cairn: {err_style(getattr(args, 'no_color', False)).red('error')}: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("cairn: interrupted", file=sys.stderr)
        return INTERRUPTED
    except Exception as e:  # noqa: BLE001 - a bug: say so briefly (CAIRN_DEBUG=1 shows where)
        if os.environ.get("CAIRN_DEBUG"):
            raise
        print(f"cairn: unexpected error: {type(e).__name__}: {e} (CAIRN_DEBUG=1 shows the details)", file=sys.stderr)
        return ERROR
    return int(rc or 0)


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["__complete"]:
        from cairn_cli.completion import run_complete

        sys.exit(run_complete(build_parser(), argv[1:]))
    args = build_parser().parse_args(argv)
    sys.exit(dispatch(args))


if __name__ == "__main__":
    main()
