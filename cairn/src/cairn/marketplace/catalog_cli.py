"""``cairn glossary …`` and ``cairn domains …``: the glossary and domains in Cairn Core, from the command line (and from
CI, or an agent's shell). Every command calls Cairn's API as you (``CAIRN_URL`` and ``CAIRN_TOKEN`` or
``CAIRN_TOKEN_FILE``, never on the command line, or the profile ``cairn login`` signed in), so your rights apply.

    cairn glossary terms [--query revenue]               # terms you may see
    cairn glossary groups
    cairn glossary apply net-revenue "urn:li:dataset:(…)" [--column amount]
    cairn glossary remove net-revenue "urn:li:dataset:(…)" [--column amount]
    cairn glossary assets net-revenue                     # the assets that carry it
    cairn glossary deprecate net-revenue --note "use revenue"
    cairn domains list
    cairn domains assets sales
    cairn domains set "urn:li:dataset:(…)" sales          # or: cairn domains unset "urn:li:dataset:(…)"

Terms, groups and domains are declared as code with ``cairn bundle`` (sections glossary_nodes, glossary_terms,
domains) or Terraform (cairn_glossary_node, cairn_glossary_term, cairn_domain).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote, urlencode


def _call():
    from cairn.flows.cli import _client

    return _client()


def _id(value: str, kind: str) -> str:
    """An id from an id or a URN (``urn:li:<kind>:<id>``), for a path."""
    prefix = f"urn:li:{kind}:"
    return quote(value[len(prefix):] if value.startswith(prefix) else value, safe="")


def _print_terms(terms: list[dict], query: Optional[str]) -> None:
    q = (query or "").lower()
    for t in terms:
        if q and q not in f"{t['id']} {t['name']} {t['definition']}".lower():
            continue
        flag = " (deprecated)" if t.get("deprecated") else ""
        first = (t.get("definition") or "").strip().split("\n")[0][:80]
        print(f"{t['id']}\t{t['name']}{flag}\t{t.get('parent') or '-'}\t{first}")


def glossary_main(a) -> int:
    from cairn.flows.cli import CliError

    try:
        call = _call()
        if a.glossary_command == "terms":
            _print_terms(call("GET", "/catalog/glossary/terms")["terms"], a.query)
        elif a.glossary_command == "groups":
            for n in call("GET", "/catalog/glossary/nodes")["nodes"]:
                print(f"{n['id']}\t{n['name']}\t{n.get('parent') or '-'}")
        elif a.glossary_command in ("apply", "remove"):
            path = "/catalog/assets/terms" + ("" if a.glossary_command == "apply" else "/remove")
            out = call("POST", path, {"asset": a.asset, "term": a.term, "field": a.column})
            where = f"{a.asset} column {a.column}" if a.column else a.asset
            verb = "on" if a.glossary_command == "apply" else "off"
            print(f"{a.term} {verb} {where}" + ("" if out.get("changed") else " (already so)"))
        elif a.glossary_command == "assets":
            for x in call("GET", f"/catalog/glossary/terms/{_id(a.term, 'glossaryTerm')}/assets")["assets"]:
                print(f"{x['urn']}\t{x['type']}\t{x['name']}")
        elif a.glossary_command == "deprecate":
            term = call("GET", f"/catalog/glossary/terms/{_id(a.term, 'glossaryTerm')}")
            body = {k: v for k, v in term.items() if k not in ("urn", "id")}
            body.update({"deprecated": not a.undo, "deprecation_note": "" if a.undo else (a.note or "")})
            call("PUT", f"/catalog/glossary/terms/{_id(a.term, 'glossaryTerm')}", body)
            print(f"{a.term} {'is no longer deprecated' if a.undo else 'deprecated'}")
        return 0
    except CliError as e:
        print(f"cairn glossary: {e}")
        return 1


def domains_main(a) -> int:
    from cairn.flows.cli import CliError

    try:
        call = _call()
        if a.domains_command == "list":
            for d in call("GET", "/catalog/domains")["domains"]:
                print(f"{d['id']}\t{d['name']}\t{d.get('parent') or '-'}\t{', '.join(d.get('owners') or [])}")
        elif a.domains_command == "assets":
            out = call("GET", f"/catalog/domains/{_id(a.domain, 'domain')}/assets")
            for x in out["products"] + out["assets"]:
                print(f"{x['urn']}\t{x['type']}\t{x['name']}")
            if out.get("hidden"):
                print(f"({out['hidden']} more you may not see)")
        elif a.domains_command in ("set", "unset"):
            domain = a.domain if a.domains_command == "set" else None
            out = call("PUT", "/catalog/assets/domain", {"asset": a.asset, "domain": domain})
            print(f"{a.asset}: {'in ' + out['domain'] if out['domain'] else 'in no domain'}"
                  + ("" if out.get("changed") else " (already so)"))
        elif a.domains_command == "delete":
            call("DELETE", f"/catalog/domains/{_id(a.domain, 'domain')}?{urlencode({'force': str(a.force).lower()})}")
            print(f"{a.domain} deleted")
        return 0
    except CliError as e:
        print(f"cairn domains: {e}")
        return 1


def cmd_glossary(a) -> None:
    raise SystemExit(glossary_main(a))


def cmd_domains(a) -> None:
    raise SystemExit(domains_main(a))


def add_parser(sub) -> None:
    g = sub.add_parser("glossary", help="the business glossary in Cairn Core: terms, groups, terms on assets")
    gs = g.add_subparsers(dest="glossary_command", required=True)
    t = gs.add_parser("terms", help="the terms you may see")
    t.add_argument("--query", help="only terms whose id, name or definition holds this")
    gs.add_parser("groups", help="the term groups")
    for name, text in (("apply", "put a term on an asset (or a column)"), ("remove", "take a term off an asset")):
        x = gs.add_parser(name, help=text)
        x.add_argument("term", help="the term's id or URN")
        x.add_argument("asset", help="the asset's URN")
        x.add_argument("--column", help="a dataset's column")
    x = gs.add_parser("assets", help="the assets that carry a term")
    x.add_argument("term")
    x = gs.add_parser("deprecate", help="mark a term deprecated (--undo: no longer)")
    x.add_argument("term")
    x.add_argument("--note", help="what to use instead")
    x.add_argument("--undo", action="store_true")
    g.set_defaults(fn=cmd_glossary)

    d = sub.add_parser("domains", help="domains in Cairn Core: list them, their assets, put assets in one")
    ds = d.add_subparsers(dest="domains_command", required=True)
    ds.add_parser("list", help="the domains you may see")
    x = ds.add_parser("assets", help="a domain's assets and data products")
    x.add_argument("domain", help="the domain's id or URN")
    x = ds.add_parser("set", help="put an asset in a domain")
    x.add_argument("asset")
    x.add_argument("domain")
    x = ds.add_parser("unset", help="take an asset out of its domain")
    x.add_argument("asset")
    x = ds.add_parser("delete", help="delete a domain (--force: take its assets and products out first)")
    x.add_argument("domain")
    x.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_domains)


__all__ = ["add_parser", "domains_main", "glossary_main"]
