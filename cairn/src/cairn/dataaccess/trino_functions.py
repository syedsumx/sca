"""Stored functions as catalog assets: each function in a routines catalog (``CAIRN_TRINO_ROUTINE_CATALOGS``)
is an asset people can find, read and own, beside the tables it is used on.

* **Learned from Trino.** Trino's event listener tells Cairn of every finished query. A finished
  ``CREATE [OR REPLACE] FUNCTION`` in a routines catalog publishes the function (its signature, return type,
  language, body and comment), owned by whoever created it; ``DROP FUNCTION`` marks it removed. Nothing
  else is needed: creating one already needs ``MANAGE_DATASET`` on it.
* **Synced.** ``POST /v1/trino/functions/sync`` (``MANAGE_INGESTION``) lists each routines catalog's
  functions as Cairn's crawler (``SHOW FUNCTIONS``), adding those created before Cairn was told and marking
  removed those that are gone.
* **The asset.** A job (subtype ``Function``) in the flow ``(trino,<catalog>,PROD)``: one per function
  name, with every signature (overloads) in its properties. ``function_urn`` is the
  ``urn:li:function:<catalog>.<schema>.<name>`` that ``USE_FUNCTION`` policies name, so the catalog page
  and the policy refer to the same function.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, delete, select

from cairn.common.db import metadata
from cairn.common.proposals import make_mcp

logger = logging.getLogger(__name__)
trino_functions = Table(
    "cairn_trino_functions", metadata,
    Column("urn", String(512), primary_key=True),          # the asset
    Column("function", String(512), nullable=False),       # urn:li:function:<catalog>.<schema>.<name>
    Column("updated_ms", BigInteger, nullable=False),
)
NAME = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
CREATE = re.compile(rf"^\s*CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+({NAME}(?:\s*\.\s*{NAME}){{0,2}})\s*\(", re.I)
# What may follow a function's return type.
CHARACTERISTICS = r"LANGUAGE|COMMENT|DETERMINISTIC|NOT|RETURNS\s+NULL|CALLED|SECURITY|WITH|RETURN|BEGIN|AS"
DROP = re.compile(rf"^\s*DROP\s+FUNCTION\s+(IF\s+EXISTS\s+)?({NAME}(?:\s*\.\s*{NAME}){{0,2}})\s*\(", re.I)


def _ident(part: str) -> str:
    part = part.strip()
    return part[1:-1] if part.startswith('"') else part.lower()


def _balanced(sql: str, start: int) -> tuple[str, int]:
    """The text inside the parentheses that open at ``start`` - 1, and the index after the closing one."""
    depth, i, quote = 1, start, None
    while i < len(sql):
        c = sql[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return sql[start:i], i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def _split(args: str) -> list[str]:
    """Top-level comma-separated items (types such as decimal(10,2) keep their commas)."""
    out, depth, cur = [], 0, ""
    for c in args:
        if c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += c == "("
        depth -= c == ")"
        cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


def types_of(arguments: str, named: bool = True) -> str:
    """A signature's argument types, normalized (``x varchar, n integer`` → ``varchar, integer``)."""
    out = []
    for a in _split(arguments):
        a = " ".join(a.split())
        if named and " " in a:
            a = a.split(" ", 1)[1]
        out.append(a.lower().replace(" (", "("))
    return ", ".join(out)


def parse_create(sql: str) -> Optional[dict]:
    """What a CREATE FUNCTION statement defines, or None if it is not one."""
    m = CREATE.match(sql)
    if not m:
        return None
    parts = [_ident(p) for p in re.split(r"\s*\.\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", m.group(2))]
    args, end = _balanced(sql, m.end())
    rest = sql[end:]
    ret = re.match(r"\s*RETURNS\s+(.+?)(?=\s+(?:" + CHARACTERISTICS + r")\b|\s*$)", rest, re.I | re.S)
    lang = re.search(r"\bLANGUAGE\s+([A-Za-z_]+)", rest, re.I)
    comment = re.search(r"\bCOMMENT\s+'((?:[^']|'')*)'", rest, re.I)
    body = re.search(r"\bAS\s+\$\$(.*?)\$\$", rest, re.S) or re.search(r"\b(RETURN\b.*|BEGIN\b.*)$", rest, re.S | re.I)
    return {"parts": parts, "arguments": " ".join(args.split()), "types": types_of(args),
            "returns": " ".join(ret.group(1).split()) if ret else "",
            "language": (lang.group(1).upper() if lang else "SQL"), "comment": comment.group(1).replace("''", "'") if comment else "",
            "body": (body.group(1) if body else "").strip()[:20000], "replace": bool(m.group(1))}


def parse_drop(sql: str) -> Optional[tuple[list[str], str]]:
    """The function a DROP FUNCTION names, and the argument types of the signature dropped."""
    m = DROP.match(sql)
    if not m:
        return None
    args, _ = _balanced(sql, m.end())
    return [_ident(p) for p in re.split(r"\s*\.\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", m.group(2))], types_of(args, named=False)


class FunctionCatalog:
    def __init__(self, engine, graph, routine_catalogs: set[str], audit=None):
        self.engine, self.graph, self.catalogs, self.audit = engine, graph, set(routine_catalogs), audit

    @staticmethod
    def function_urn(catalog: str, schema: str, name: str) -> str:
        return f"urn:li:function:{catalog}.{schema}.{name}"

    @staticmethod
    def flow_urn(catalog: str) -> str:
        return f"urn:li:dataFlow:(trino,{catalog},PROD)"

    def asset_urn(self, catalog: str, schema: str, name: str) -> str:
        return f"urn:li:dataJob:({self.flow_urn(catalog)},{schema}.{name})"

    def _full(self, parts: list[str], default_catalog: str = "", default_schema: str = "") -> Optional[tuple[str, str, str]]:
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2 and default_catalog:
            return default_catalog, parts[0], parts[1]
        if len(parts) == 1 and default_catalog and default_schema:
            return default_catalog, default_schema, parts[0]
        return None

    def publish(self, catalog: str, schema: str, name: str, signatures: list[dict], owner: Optional[str], comment: str = "",
                actor: str = "") -> str:
        urn, now = self.asset_urn(catalog, schema, name), int(time.time() * 1000)
        language = sorted({s.get("language") or "SQL" for s in signatures})
        props = {"function_urn": self.function_urn(catalog, schema, name), "catalog": catalog, "schema": schema,
                 "language": ",".join(language),
                 "signatures": json.dumps([{k: s.get(k, "") for k in ("arguments", "types", "returns", "language")}
                                           for s in signatures]),
                 "signature": "; ".join(f"{name}({s.get('arguments', '')}) → {s.get('returns', '')}" for s in signatures)}
        body = next((s.get("body") for s in signatures if s.get("body")), "")
        if body:
            props["body"] = body[:20000]
        mcps = [
            make_mcp(self.flow_urn(catalog), "dataFlowInfo", {"name": f"{catalog} functions", "customProperties": {}}),
            make_mcp(urn, "dataJobInfo", {"name": f"{catalog}.{schema}.{name}", "description": comment or None,
                                          "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                                          "flowUrn": self.flow_urn(catalog), "customProperties": props}),
            make_mcp(urn, "subTypes", {"typeNames": ["Function"]}),
            make_mcp(urn, "status", {"removed": False}),
        ]
        if owner:
            mcps.append(make_mcp(urn, "ownership", {"owners": [{"owner": owner, "type": "TECHNICAL_OWNER"}],
                                                     "lastModified": {"time": now, "actor": actor or owner}}))
        self.graph.emit(mcps)
        with self.engine.begin() as conn:
            conn.execute(delete(trino_functions).where(trino_functions.c.urn == urn))
            conn.execute(trino_functions.insert().values(urn=urn, function=props["function_urn"], updated_ms=now))
        if self.audit is not None:
            self.audit.record("catalog", "function.published", actor or owner or "cairn", urn,
                              {"function": props["function_urn"], "signatures": len(signatures)})
        return urn

    def remove(self, catalog: str, schema: str, name: str, actor: str = "cairn") -> str:
        return self._remove(self.asset_urn(catalog, schema, name), self.function_urn(catalog, schema, name), actor)

    def _remove(self, urn: str, function: str, actor: str) -> str:
        self.graph.emit([make_mcp(urn, "status", {"removed": True})])
        with self.engine.begin() as conn:
            conn.execute(delete(trino_functions).where(trino_functions.c.urn == urn))
        if self.audit is not None:
            self.audit.record("catalog", "function.removed", actor, urn, {"function": function})
        return urn

    def _signatures(self, urn: str) -> list[dict]:
        info = self.graph.get_aspect(urn, "dataJobInfo") or {}
        try:
            return json.loads((info.get("customProperties") or {}).get("signatures") or "[]")
        except ValueError:
            return []

    def from_event(self, event: dict) -> Optional[str]:
        """Trino's event for a finished query: a function created or dropped in a routines catalog."""
        meta, ctx = event.get("metadata") or {}, event.get("context") or {}
        if event.get("failureInfo") or meta.get("queryState") not in (None, "FINISHED"):
            return None
        sql, user = str(meta.get("query") or ""), str(ctx.get("user") or "")
        if "function" not in sql[:200].lower():
            return None
        try:
            created = parse_create(sql)
            dropped = None if created else parse_drop(sql)
        except ValueError:
            return None
        parts = created["parts"] if created else (dropped[0] if dropped else None)
        if not parts:
            return None
        full = self._full(parts, str(ctx.get("catalog") or ""), str(ctx.get("schema") or ""))
        if full is None or full[0] not in self.catalogs:
            return None
        catalog, schema, name = full
        actor = f"urn:li:corpuser:{user}" if user else "cairn"
        urn = self.asset_urn(catalog, schema, name)
        status = self.graph.get_aspect(urn, "status") or {}
        if dropped:
            left = [s for s in self._signatures(urn) if s.get("types") != dropped[1]] if not status.get("removed") else []
            if not left:
                return self.remove(catalog, schema, name, actor)
            info = self.graph.get_aspect(urn, "dataJobInfo") or {}
            return self.publish(catalog, schema, name, left, None, info.get("description") or "", actor)   # one overload gone
        sig = {k: created[k] for k in ("arguments", "types", "returns", "language", "body")}
        known = [] if status.get("removed") else [s for s in self._signatures(urn) if s.get("types") != sig["types"]]
        owners = (self.graph.get_aspect(urn, "ownership") or {}).get("owners") or []
        owner = None if owners and not status.get("removed") else actor      # an existing function keeps its owner
        return self.publish(catalog, schema, name, known + [sig], owner, created["comment"], actor)

    def sync(self, run: Callable[[str], list[list[Any]]]) -> dict:
        """Every function in the routines catalogs, as Trino lists them (``run(sql)``: rows, as Cairn's
        crawler); those gone are marked removed."""
        seen: set[str] = set()
        added = 0
        for catalog in sorted(self.catalogs):
            for (schema,) in run(f'SHOW SCHEMAS FROM "{catalog}"'):
                if schema == "information_schema":
                    continue
                rows = run(f'SHOW FUNCTIONS FROM "{catalog}"."{schema}"')
                by: dict[str, list[dict]] = {}
                comments: dict[str, str] = {}
                for r in rows:            # Function, Return Type, Argument Types, Function Type, Deterministic, Description
                    by.setdefault(str(r[0]), []).append({"arguments": str(r[2]), "types": types_of(str(r[2]), named=False),
                                                         "returns": str(r[1]), "language": "SQL"})
                    if len(r) > 5 and r[5]:
                        comments[str(r[0])] = str(r[5])
                for name, sigs in by.items():
                    urn = self.asset_urn(catalog, schema, name)
                    seen.add(urn)
                    status = self.graph.get_aspect(urn, "status") or {}
                    if self.graph.get_aspect(urn, "dataJobInfo") and not status.get("removed"):
                        known = {s.get("types"): s for s in self._signatures(urn)}
                        if set(known) == {s["types"] for s in sigs}:
                            continue                      # as Cairn has it (with the body it learned)
                        sigs = [known.get(s["types"]) or s for s in sigs]
                    else:
                        added += 1
                    self.publish(catalog, schema, name, sigs, None, comments.get(name, ""), "cairn")
        with self.engine.connect() as conn:
            gone = [(r.urn, r.function) for r in conn.execute(select(trino_functions)) if r.urn not in seen]
        for urn, function in gone:
            self._remove(urn, function, "cairn")
        return {"functions": len(seen), "added": added, "removed": len(gone)}
