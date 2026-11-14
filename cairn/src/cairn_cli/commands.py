"""The commands for people: ``query``, ``search``, ``show``, ``ask``, and Cairn's other groups under one set of verbs
(``iac plan|apply|validate|export``, ``ontology status|compile``). Everything runs as the signed-in person; Cairn
decides what they may do."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from cairn_cli.errors import INTERRUPTED, NOT_FOUND, USAGE, CliError
from cairn_cli.output import Style, emit_json, err_style, table, writer_for
from cairn_cli.session import Session


def _note(a, text: str) -> None:
    if not getattr(a, "quiet", False):
        print(text, file=sys.stderr)


# ---- query -------------------------------------------------------------------------------------------------
def _sql_of(a) -> str:
    if a.file:
        try:
            with (sys.stdin if a.file == "-" else open(a.file, encoding="utf-8")) as fh:
                return fh.read()
        except OSError as e:
            raise CliError(f"{a.file}: {e.strerror}", USAGE)
    if a.sql and a.sql != "-":
        return a.sql
    if a.sql == "-" or not sys.stdin.isatty():
        return sys.stdin.read()
    raise CliError("give the SQL: cairn query \"SELECT …\", -f file.sql, or on standard input", USAGE)


def cmd_query(a) -> int:
    sql = _sql_of(a).strip().rstrip(";").strip()
    if not sql:
        raise CliError("the SQL is empty", USAGE)
    fmt = a.format or ("json" if a.output == "json" else ("table" if sys.stdout.isatty() and not a.out else "csv"))
    out_stream = None
    if a.out and fmt != "parquet":
        out_stream = open(a.out, "w", encoding="utf-8", newline="")
    stream = out_stream or sys.stdout
    writer = writer_for(fmt, stream, Style(stream, a.no_color), a.out if fmt == "parquet" else None)
    s = Session.open(a.profile)
    page: dict = {}
    started, total, masked = time.monotonic(), 0, set()
    try:
        page = s.call("POST", "/v1/statements", {"sql": sql, "limit": a.limit, "warehouse": a.warehouse,
                                                 "connection": a.connection}, timeout=120)
        begun = False
        while True:
            if page.get("columns") and not begun:
                writer.start(page["columns"])
                begun = True
            rows = page.get("rows") or []
            if rows:
                if not begun:
                    raise CliError("Cairn sent rows before their columns")
                if a.limit and total + len(rows) > a.limit:
                    rows = rows[: a.limit - total]
                writer.write(rows)
                total += len(rows)
                masked |= set(page.get("masked_columns") or [])
            if not page.get("next") or (a.limit and total >= a.limit):
                break
            page = s.call("POST", "/v1/statements/next", {"cursor": page["next"]}, timeout=120)
        if not begun:
            writer.start([])
        writer.finish()
        if page.get("next"):                       # stopped at --limit: free Trino's resources now
            _cancel(s, page["next"])
    except KeyboardInterrupt:
        if page.get("next"):
            _cancel(s, page["next"])
            print("\ncairn: query cancelled in Cairn", file=sys.stderr)
        raise SystemExit(INTERRUPTED)
    except BrokenPipeError:                         # `cairn query … | head`: the reader has what it wanted
        if page.get("next"):
            _cancel(s, page["next"])
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())   # no second error when Python flushes
        return 0
    finally:
        s.close()
        if out_stream is not None:
            out_stream.close()
    if masked:
        _note(a, err_style(a.no_color).yellow(f"masked for you: {', '.join(sorted(masked))}"))
    if sys.stderr.isatty() or a.out:
        _note(a, err_style(a.no_color).dim(f"{total} row{'s' if total != 1 else ''} in "
                                           f"{time.monotonic() - started:.1f} s" + (f" → {a.out}" if a.out else "")))
    return 0


def _cancel(s: Session, cursor: str) -> None:
    try:
        s.request("POST", "/v1/statements/cancel", json={"cursor": cursor}, timeout=15, ok=(404, 409, 410))
    except (CliError, KeyboardInterrupt):
        pass


# ---- search, show, ask ------------------------------------------------------------------------------------
def cmd_search(a) -> int:
    s = Session.open(a.profile)
    try:
        params: list[tuple[str, Any]] = [("q", " ".join(a.text)), ("limit", a.limit)] + [("type", t) for t in a.type or []]
        found = s.call("GET", "/v1/assets/search", params=params)["results"]
    finally:
        s.close()
    if a.output == "json":
        emit_json(found)
        return 0
    if not found:
        _note(a, "Nothing found that you may see.")
        return 0
    st = Style(sys.stdout, a.no_color)
    print(table(["TYPE", "NAME", "PLATFORM", "URN"],
                [[(r.get("type") or "").lower(), r.get("name") or "", r.get("platform") or "", r["urn"]] for r in found], st))
    return 0


def cmd_show(a) -> int:
    s = Session.open(a.profile)
    try:
        params = {"urn": a.asset} if a.asset.startswith("urn:li:") else {"name": a.asset}
        r = s.request("GET", "/v1/assets/describe", params=params, ok=(409,))
        doc = r.json()
    finally:
        s.close()
    if r.status_code == 409:
        lines = [f"  {c['urn']}" for c in doc.get("candidates") or []]
        raise CliError("\n".join([doc.get("detail") or "several assets match", *lines]), USAGE)
    if a.output == "json":
        emit_json(doc)
        return 0
    st = Style(sys.stdout, a.no_color)
    short = lambda u: u.rsplit(":", 1)[-1] if u.startswith(("urn:li:corpuser:", "urn:li:corpGroup:", "urn:li:tag:",   # noqa: E731
                                                              "urn:li:glossaryTerm:", "urn:li:domain:")) else u
    print(st.bold(doc["name"]) + (st.dim(f"  ({doc['platform']})") if doc.get("platform") else ""))
    print(st.dim(doc["urn"]))
    if doc.get("description"):
        print("\n" + doc["description"])
    facts = [("Owners", ", ".join(short(o["owner"]) for o in doc.get("owners") or [])),
             ("Terms", ", ".join(short(t) for t in doc.get("terms") or [])),
             ("Tags", ", ".join(short(t) for t in doc.get("tags") or [])),
             ("Domain", ", ".join(short(d) for d in doc.get("domains") or []))]
    print()
    for k, v in facts:
        print(f"{k + ':':<9} {v or st.dim('-')}")
    cols = doc.get("columns") or []
    if cols:
        print("\n" + st.bold(f"Schema ({len(cols)} columns)"))
        print(table(["COLUMN", "TYPE", "TERMS", "DESCRIPTION"],
                    [[c["name"], c.get("type") or "", ", ".join(short(t) for t in c.get("terms") or []),
                      c.get("description") or ""] for c in cols], st))
    lin = doc.get("lineage") or {}
    up, down = lin.get("upstream") or [], lin.get("downstream")
    print("\n" + st.bold("Lineage"))
    print(f"  built from: {len(up)}" + "".join(f"\n    {u}" for u in up[:10]) + ("\n    …" if len(up) > 10 else ""))
    if down is None:
        print("  feeds: unknown (the catalog's lineage search is not available)")
    else:
        print(f"  feeds: {len(down)}" + "".join(f"\n    {d}" for d in down[:10]) + ("\n    …" if len(down) > 10 else ""))
    return 0


def cmd_ask(a) -> int:
    s = Session.open(a.profile)
    try:
        doc = s.call("POST", "/data/ask", {"question": " ".join(a.question), "urns": a.dataset or [],
                                           "run": not a.no_run, "limit": a.limit}, timeout=300)
    finally:
        s.close()
    if a.output == "json":
        emit_json(doc)
        return 0 if not doc.get("error") else 1
    st = Style(sys.stdout, a.no_color)
    if doc.get("sql"):
        print(st.bold("SQL") + st.dim(f"  ({doc.get('connection')}, {doc.get('dialect')})"))
        print(st.cyan(doc["sql"]))
    if doc.get("explanation"):
        print("\n" + doc["explanation"])
    for f in (doc.get("ontology") or {}).get("findings") or []:
        print(st.yellow(f"note: {f.get('message') or f}"))
    res = doc.get("result")
    if res:
        print()
        print(table(res["columns"], res["rows"], st))
        if res.get("masked_columns"):
            _note(a, st.yellow(f"masked for you: {', '.join(res['masked_columns'])}"))
        if res.get("truncated"):
            _note(a, st.dim(f"(the first {len(res['rows'])} rows)"))
    if doc.get("error"):
        raise CliError(f"no answer: {doc['error']}")
    return 0


# ---- the other groups, through the signed-in profile --------------------------------------------------------
def cmd_iac(a) -> int:
    """``iac validate|plan|apply`` are ``cairn bundle``; ``iac export`` is ``cairn export``, as the signed-in person."""
    if a.iac_cmd != "validate":
        from cairn_cli.session import api_credentials

        url, token, verify = api_credentials(a.profile)
        a.url, a.token = url, token
        if isinstance(verify, str):
            os.environ.setdefault("CAIRN_CA_FILE", verify)
    if a.iac_cmd == "export":
        from cairn.iac.export import main as export_main

        return export_main(a)
    from cairn.iac.bundle import main as bundle_main

    a.action = a.iac_cmd
    return bundle_main(a)


def cmd_ontology(a) -> int:
    s = Session.open(a.profile)
    try:
        if a.ontology_cmd == "status":
            try:
                doc = s.call("GET", "/v1/ontology/status")
            except CliError as e:
                if e.code == NOT_FOUND:
                    raise CliError("Cairn Ontology is not set up on this Cairn (CAIRN_ONTOLOGY_URL)", NOT_FOUND)
                raise
        else:                                       # compile: syncs first; --wait imports the run's proposals
            doc = s.call("POST", "/v1/ontology/runs", {"mode": a.mode, "sync": True, "wait": a.wait},
                         timeout=3600 if a.wait else 120)
    finally:
        s.close()
    emit_json(doc)
    return 0


def cmd_flows(a) -> int:
    from cairn.flows.cli import main as flows_main

    return flows_main(a)
