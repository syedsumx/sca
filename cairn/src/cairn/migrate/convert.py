"""SQL from Snowflake and Databricks, converted to Cairn's dialect (Trino over Iceberg), with a report (L170).

``convert(sql, source)`` takes a script and answers statement by statement:

* ``converted``: the statement in Cairn's dialect. ``verified`` says whether Trino itself accepted it (when the caller
  gave a ``check``: Cairn's API does, for queries over tables that exist); unverified statements are a translation,
  not a promise.
* ``needs_review``: a translation that is known to differ from the original, or that Trino refused, with why: an
  identity column (Iceberg has none), a ``FLATTEN`` whose output columns are used, a format string Cairn could not
  read, ...
* ``not_converted``: there is no such statement in Cairn (a stream, a task, a stage, ``COPY INTO``, ``OPTIMIZE``, a
  grant), with the Cairn feature that does that job instead.

Nothing is dropped silently: every change that is more than spelling is a note on its statement. The parsing and most
of the function mapping is sqlglot's; this module adds what it leaves wrong for Trino over Iceberg (types, table
properties, identity columns, format strings, JSON objects) and the report.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

SOURCES = {"snowflake": "snowflake", "databricks": "databricks"}
MAX_SQL = 2_000_000
MAX_STATEMENTS = 5000

# Statements with no counterpart: (pattern on the statement's start, what does that job in Cairn).
ELSEWHERE = [
    (r"CREATE\s+(OR\s+REPLACE\s+)?STREAM\b", "stream", "a table's change feed (GET /v1/lakehouse/changes) or a mirror reads changes"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?TASK\b|ALTER\s+TASK\b|EXECUTE\s+TASK\b", "task",
     "a SQL workflow or a flow runs statements on a schedule"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?(TEMPORARY\s+)?STAGE\b", "stage", "a governed volume holds files; a pipe loads them"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?PIPE\b", "pipe", "a pipe (POST /v1/pipes) loads files as they arrive"),
    (r"COPY\s+INTO\b", "copy", "a pipe (POST /v1/pipes) loads files into a table"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?FILE\s+FORMAT\b", "file_format", "a pipe's own format settings"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?WAREHOUSE\b|ALTER\s+WAREHOUSE\b|USE\s+WAREHOUSE\b", "warehouse",
     "warehouses (/v1/warehouses) are set up outside SQL"),
    (r"USE\s+ROLE\b|CREATE\s+(OR\s+REPLACE\s+)?ROLE\b|GRANT\b|REVOKE\b", "grant",
     "access is decided by Cairn's policies, not by grants in SQL"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?(MASKING|ROW\s+ACCESS)\s+POLICY\b|ALTER\s+TABLE\b.*\bSET\s+(MASKING|ROW\s+ACCESS)\s+POLICY\b",
     "policy", "column masks and row filters are Cairn policies"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?(DYNAMIC\s+TABLE|STREAMING\s+TABLE|LIVE\s+TABLE)\b", "dynamic_table",
     "a derived table (/v1/derived) is kept fresh from its query"),
    (r"OPTIMIZE\b|VACUUM\b|ANALYZE\s+TABLE\b", "maintenance", "table maintenance compacts, expires and clusters tables itself"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?(SHARE|RECIPIENT)\b", "share", "shares (POST /v1/shares)"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?SEQUENCE\b", "sequence", "Iceberg has no sequences: generate keys in the load"),
    (r"(CREATE|ALTER)\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION)\b.*\bLANGUAGE\s+(JAVASCRIPT|JAVA|SCALA|PYTHON)\b", "routine",
     "rewrite as a SQL or Python function (CREATE FUNCTION), or a procedure (/v1/procedures)"),
    (r"CREATE\s+(OR\s+REPLACE\s+)?PROCEDURE\b|CALL\b", "procedure", "a procedure (/v1/procedures): typed parameters and statements"),
    (r"USE\s+(DATABASE|CATALOG|SCHEMA)\b|USE\b", "use", "name tables as schema.table; the catalog is the connection's"),
    (r"(PUT|GET|LIST|REMOVE)\s+['@]", "stage_file", "files are in governed volumes (/v1/volumes)"),
    (r"ALTER\s+SESSION\b|SET\s+\w+\s*=", "session", "session settings do not carry over"),
    (r"REFRESH\s+TABLE\b|MSCK\b|CACHE\s+TABLE\b|UNCACHE\b", "cache", "not needed: tables are read from the catalog's current snapshot"),
    (r"COMMENT\s+ON\b", "comment", "descriptions are kept in the catalog (the asset's page)"),
]
# Snowflake format elements → Java/Joda patterns (Trino's format_datetime), longest first.
FORMAT = [("YYYY", "yyyy"), ("YY", "yy"), ("MMMM", "MMMM"), ("MON", "MMM"), ("MM", "MM"), ("DD", "dd"), ("DY", "EEE"),
          ("HH24", "HH"), ("HH12", "hh"), ("HH", "HH"), ("MI", "mm"), ("SS", "ss"), ("FF3", "SSS"), ("AM", "a"), ("PM", "a")]


def _format(pattern: str) -> Optional[str]:
    """A Snowflake date format as a format_datetime pattern, or None when an element is not known."""
    out, i, up = "", 0, pattern.upper()
    while i < len(pattern):
        for src, dst in FORMAT:
            if up.startswith(src, i):
                out, i = out + dst, i + len(src)
                break
        else:
            c = pattern[i]
            if c.isalpha():
                return None
            out, i = out + ("''" if c == "'" else c), i + 1
    return out


def _note(notes: list, code: str, message: str, review: bool = False) -> None:
    if not any(n["code"] == code for n in notes):
        notes.append({"code": code, "message": message, "review": review})


def _rewrite(tree, source: str, notes: list):
    """What sqlglot leaves wrong for Trino over Iceberg. Every change that is more than spelling leaves a note."""
    from sqlglot import exp

    def fix(node):
        if isinstance(node, exp.DataType):
            t = node.this
            column = isinstance(node.parent, exp.ColumnDef)      # Iceberg tables keep JSON as text
            if t in (exp.DataType.Type.VARIANT, exp.DataType.Type.OBJECT) or \
                    (t == exp.DataType.Type.ARRAY and not node.expressions and source == "snowflake"):
                _note(notes, "variant", "VARIANT, OBJECT and untyped ARRAY columns become VARCHAR holding JSON text (JSON in "
                                        "expressions): read values with json_extract or json_value")
                return exp.DataType.build("VARCHAR" if column else "JSON")
            if t == exp.DataType.Type.TIMESTAMPLTZ:           # an instant, shown in the session's zone
                return exp.DataType.build("TIMESTAMPTZ")
            if t in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.NVARCHAR) and node.expressions:
                size = node.expressions[0].name if node.expressions else ""
                if size.isdigit() and int(size) >= 65535:      # a maximum, not a meaning
                    return exp.DataType.build("VARCHAR")
        if isinstance(node, exp.ColumnDef):
            for c in list(node.args.get("constraints") or []):
                kind = c.args.get("kind")
                if isinstance(kind, (exp.AutoIncrementColumnConstraint, exp.GeneratedAsIdentityColumnConstraint)):
                    node.args["constraints"].remove(c)
                    _note(notes, "identity", f"column {node.name}: Iceberg has no identity columns, so it is a plain column "
                                             "here; generate its values in the load", True)
                elif isinstance(kind, (exp.PrimaryKeyColumnConstraint, exp.UniqueColumnConstraint)):
                    node.args["constraints"].remove(c)
                    _note(notes, "keys", "primary and unique keys are not enforced and were left out: check them with a "
                                         "quality monitor")
                elif isinstance(kind, (exp.DefaultColumnConstraint, exp.CollateColumnConstraint)):
                    node.args["constraints"].remove(c)
                    _note(notes, "defaults", f"column {node.name}: its DEFAULT or COLLATE was left out (not supported on "
                                             "Iceberg tables here)", True)
        if isinstance(node, exp.ToChar) or (isinstance(node, exp.Anonymous) and node.name.upper() in ("TO_VARCHAR", "TO_CHAR")):
            args = [node.this, node.args.get("format")] if isinstance(node, exp.ToChar) else list(node.expressions)
            if len(args) == 2 and isinstance(args[1], exp.Literal) and args[1].is_string:
                joda = _format(args[1].this)
                if joda is not None and re.search(r"[yMdHhms]", joda):
                    return exp.Anonymous(this="format_datetime", expressions=[args[0], exp.Literal.string(joda)])
                _note(notes, "format", f"the format {args[1].this!r} was not understood: write it for format_datetime "
                                       "(dates) or format (numbers)", True)
            elif len(args) == 1 or args[1] is None:
                return exp.Cast(this=args[0], to=exp.DataType.build("VARCHAR"))
        if isinstance(node, exp.Struct) and source == "snowflake" and node.expressions and \
                all(isinstance(e, exp.PropertyEQ) for e in node.expressions):
            _note(notes, "object", "OBJECT_CONSTRUCT becomes json_object: the result is JSON")
            return exp.JSONObject(expressions=[exp.JSONKeyValue(this=exp.Literal.string(e.this.name), expression=e.expression)
                                               for e in node.expressions])
        if isinstance(node, exp.Explode) or (isinstance(node, exp.Anonymous) and node.name.upper() == "FLATTEN") or \
                (isinstance(node, exp.Unnest) and any(isinstance(e, exp.Kwarg) for e in node.expressions)):
            if source == "snowflake":
                _note(notes, "flatten", "FLATTEN: rewrite with CROSS JOIN UNNEST(CAST(<json> AS ARRAY(JSON))) AS t(value); "
                                        "its other outputs (key, index, path) have no counterpart", True)
        return node
    tree = tree.transform(fix)
    # QUALIFY: Trino has none. The filter moves around a subquery; the statement's ORDER BY and LIMIT apply after it,
    # so they stay outside (inside, the order would be lost and the limit would cut before the filter).
    from sqlglot.transforms import eliminate_qualify

    for sel in [s for s in tree.find_all(exp.Select) if s.args.get("qualify")]:
        order, limit, offset = sel.args.get("order"), sel.args.get("limit"), sel.args.get("offset")
        names = {e.alias_or_name.lower() for e in sel.expressions if e.alias_or_name}
        star = any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in sel.expressions)
        if order is not None and not star and any(
                not isinstance(o.this, exp.Literal) and (not isinstance(o.this, exp.Column) or o.this.name.lower() not in names)
                for o in order.expressions):
            _note(notes, "qualify_order", "QUALIFY with an ORDER BY on something the query does not return: add it to the "
                                          "SELECT list, or order in an outer query", True)
            continue
        for key in ("order", "limit", "offset"):
            sel.set(key, None)
        outer = eliminate_qualify(sel.copy())
        if order is not None:
            for o in order.expressions:
                if isinstance(o.this, exp.Column):
                    o.this.set("table", None)            # the outer query knows the columns by their names alone
            outer.set("order", order)
        outer.set("limit", limit)
        outer.set("offset", offset)
        if sel is tree:
            tree = outer
        else:
            sel.replace(outer)

    if isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() == "TABLE":
        props = tree.args.get("properties")
        keep, extra = [], []
        for p in (props.expressions if props else []):
            if isinstance(p, getattr(exp, "ClusterProperty", exp.Cluster)):
                cols = [e.name for e in p.expressions if isinstance(e, exp.Column)]
                if cols and len(cols) == len(p.expressions):
                    extra.append(("sorted_by", cols))
                    _note(notes, "cluster", "CLUSTER BY becomes the table's sort order (sorted_by); Cairn also clusters "
                                            "tables from what their queries filter on")
                else:
                    _note(notes, "cluster", "CLUSTER BY on expressions was left out: Cairn clusters tables from what "
                                            "their queries filter on", True)
            elif isinstance(p, exp.PartitionedByProperty):
                cols = [e.name for e in p.this.expressions] if isinstance(p.this, (exp.Schema, exp.Tuple)) else []
                if cols and all(cols):
                    extra.append(("partitioning", cols))
                else:
                    _note(notes, "partition", "PARTITIONED BY was not understood and was left out", True)
            elif isinstance(p, exp.FileFormatProperty):
                _note(notes, "format_iceberg", "the table is an Iceberg table here, whatever its format was (Delta, Parquet)")
            elif not isinstance(p, (exp.SchemaCommentProperty, exp.TemporaryProperty)):
                _note(notes, "properties", "table properties and locations were left out: they are the source's own "
                                           "settings (retention, auto-optimise, storage path)")
            elif isinstance(p, exp.TemporaryProperty):
                _note(notes, "temporary", "a temporary table becomes an ordinary one: drop it when done", True)
            else:
                keep.append(p)
        for name, cols in extra:
            keep.append(exp.Property(this=exp.var(name), value=exp.Array(expressions=[exp.Literal.string(c) for c in cols])))
        if props is not None or keep:
            tree.set("properties", exp.Properties(expressions=keep) if keep else None)
        if tree.args.get("clone"):
            _note(notes, "clone", "CLONE: use the table clone API (zero-copy clone of an Iceberg table)", True)
    return tree


def _leftovers(sql: str, notes: list) -> None:
    """Spellings Trino does not have that can survive a translation: said rather than shipped."""
    for pattern, code, message in (
            (r"\bAUTO_INCREMENT\b|\bGENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b", "identity", "an identity column remains"),
            (r"\bVARIANT\b", "variant", "a VARIANT type remains"),
            (r"\bCLUSTER\s+BY\b", "cluster", "CLUSTER BY remains"),
            (r"=>", "named_args", "a named argument (=>) remains: Trino functions take positional arguments"),
            (r"\bDATE_FORMAT\([^()]*,\s*'[^'%]*[A-Za-z][^'%]*'\)", "format", "a date format that is not in MySQL's % form remains"),
            (r"\bINSERT\s+OVERWRITE\b", "overwrite", "INSERT OVERWRITE remains (Trino has none: DELETE then INSERT, or "
                                                    "CREATE OR REPLACE TABLE ... AS)"),
            (r"\bCLONE\b", "clone", "CLONE remains"),
            (r"\bLATERAL\s+VIEW\b", "lateral_view", "LATERAL VIEW remains"),
            (r"\$\d+\b", "positional", "a positional column ($1) remains: name the column")):
        if re.search(pattern, sql, re.I):
            _note(notes, code, message + " in the translation", True)


def split(sql: str, dialect: str) -> list[str]:
    """The script's statements, split at the semicolons that end them (not those in strings, comments or bodies)."""
    from sqlglot import tokenize
    from sqlglot.tokens import TokenType

    out, start, depth = [], 0, 0
    try:
        tokens = tokenize(sql, read=dialect)
    except Exception:  # noqa: BLE001 - not tokenisable as a whole: the simplest split, each piece judged on its own
        return [s.strip() for s in sql.split(";") if s.strip()]
    for t in tokens:
        if t.token_type == TokenType.BEGIN or (t.token_type == TokenType.VAR and t.text.upper() == "BEGIN"):
            depth += 1
        elif t.token_type == TokenType.END and depth:
            depth -= 1
        elif t.token_type == TokenType.SEMICOLON and depth == 0:
            piece = sql[start:t.start].strip()
            if piece:
                out.append(piece)
            start = t.end + 1
    tail = sql[start:].strip()
    if tail:
        out.append(tail)
    return out


CONTROL = re.compile(r"\b(DECLARE|IF|ELSEIF|ELSIF|FOR|WHILE|LOOP|REPEAT|CASE|EXCEPTION|CURSOR|LET|CALL|RESULTSET|SIGNAL|"
                     r"EXECUTE\s+IMMEDIATE|OPEN|FETCH|SET)\b|:=", re.I)
PARAM_TYPES = {"INT": "integer", "BIGINT": "integer", "SMALLINT": "integer", "TINYINT": "integer", "DOUBLE": "double",
               "FLOAT": "double", "VARCHAR": "varchar", "TEXT": "varchar", "CHAR": "varchar", "NVARCHAR": "varchar",
               "BOOLEAN": "boolean", "DATE": "date"}


def convert_procedure(sql: str, source: str) -> Optional[dict]:
    """A SQL procedure whose body is statements run in order, as a Cairn procedure (``/v1/procedures``: typed
    parameters, statements with ``:name``). One with control flow, variables or dynamic SQL is not a list of
    statements: it is said, and nothing is produced. None: not a SQL procedure at all."""
    import sqlglot
    from sqlglot import errors, exp

    if re.search(r"\bLANGUAGE\s+(JAVASCRIPT|JAVA|SCALA|PYTHON)\b", sql, re.I):
        return None
    out = {"source_sql": sql, "status": "needs_review", "sql": None, "kind": "procedure", "verified": False, "notes": []}
    notes = out["notes"]
    try:
        tree = sqlglot.parse_one(sql, read=SOURCES[source], error_level=errors.ErrorLevel.RAISE)
        head = tree.this
        full = head.this.sql(dialect="trino").replace('"', "")
        params = [(c.name.lower(), c.args.get("kind")) for c in head.expressions]
    except Exception:  # noqa: BLE001 - a header that does not read: reported like any statement that does not
        return None
    m = re.search(r"\bAS\s+\$\$(.*)\$\$\s*;?\s*$", sql, re.S | re.I) or re.search(r"\bAS\s+'(.*)'\s*;?\s*$", sql, re.S | re.I) \
        or re.search(r"\b(BEGIN\b.*)$", sql, re.S | re.I)
    body = (m.group(1) if m else "").strip().rstrip(";").strip()
    bare = re.sub(r"'(?:[^']|'')*'", "''", re.sub(r"--[^\n]*|/\*.*?\*/", " ", body, flags=re.S))
    flow = sorted({w.upper().split()[0] for w in CONTROL.findall(bare) if w} | ({":="} if ":=" in bare else set()))
    if flow:
        _note(notes, "procedure_logic", f"the body has logic beyond statements in order ({', '.join(flow)}): a Cairn "
                                        "procedure is statements with parameters. Use a flow (steps, conditions, "
                                        "loops) or a Python step for this one", True)
        return out
    inner = re.match(r"^BEGIN\b(.*)\bEND$", body, re.S | re.I)
    if not inner:
        _note(notes, "procedure_body", "the procedure's body is not BEGIN ... END with statements: rewrite it as a "
                                       "procedure (/v1/procedures) or a flow", True)
        return out
    body = inner.group(1)
    typed = []
    for name, kind in params:
        base = kind.this.name if kind is not None else ""
        ptype = PARAM_TYPES.get(base)
        if ptype is None and base in ("DECIMAL", "NUMBER", "NUMERIC"):
            scale = kind.expressions[1].name if len(kind.expressions) > 1 else "0"
            ptype = "integer" if scale == "0" else "double"
        if ptype is None:
            ptype = "varchar"
            _note(notes, "procedure_param", f"parameter {name} ({kind.sql() if kind is not None else '?'}) has no "
                                            "counterpart type and is passed as varchar: cast it in the statements", True)
        typed.append({"name": name, "type": ptype})
    names = {p["name"] for p in typed}
    statements = []
    for piece in split(body, SOURCES[source]):
        if re.match(r"^RETURN\b", piece, re.I):
            if re.match(r"^RETURN\s+TABLE\b", piece, re.I):
                _note(notes, "procedure_return", "RETURN TABLE(...): make the query the procedure's last statement", True)
            else:
                _note(notes, "procedure_return", "the RETURN value is dropped: a Cairn procedure returns its last "
                                                 "statement's rows")
            continue
        if source == "databricks":                       # parameters are bare names there: :name here
            try:
                t = sqlglot.parse_one(piece, read="databricks", error_level=errors.ErrorLevel.RAISE)
                hit = [c for c in t.find_all(exp.Column) if not c.table and c.name.lower() in names]
                for c in hit:
                    c.replace(exp.Placeholder(this=c.name.lower()))
                if hit:
                    _note(notes, "procedure_names", "names that are parameters were read as parameters ("
                                                    + ", ".join(sorted({c.name.lower() for c in hit}))
                                                    + "): check none of them was meant as a column", True)
                    piece = t.sql(dialect="databricks")
            except errors.SqlglotError:
                pass
        st = convert_statement(piece, source)
        for n in st["notes"]:
            _note(notes, n["code"], n["message"], n["review"])
        if not st["sql"]:
            _note(notes, "procedure_statement", f"a statement in the body does not convert: {' '.join(piece.split())[:80]!r}", True)
            return out
        statements.append(st["sql"])
    if not statements:
        _note(notes, "procedure_body", "the body has no statement to run", True)
        return out
    used = set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", " ".join(re.sub(r"'(?:[^']|'')*'", "''", s) for s in statements)))
    if not {u.lower() for u in used} <= names:
        _note(notes, "procedure_param", f"the body uses :{sorted(used - names)[0]}, which is not a parameter", True)
        return out
    name = re.sub(r"[^a-z0-9_]", "_", full.split(".")[-1].lower())
    out["procedure"] = {"name": name, "params": typed, "statements": statements,
                        "description": f"Converted from the {source} procedure {full}"}
    _note(notes, "procedure", "a Cairn procedure: create it with POST /v1/procedures (add the connection). It runs as "
                              "whoever calls it, never with its owner's rights, and its statements are not one transaction")
    out["status"] = "needs_review" if any(n["review"] for n in notes) else "converted"
    return out


def convert_statement(sql: str, source: str, check: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """One statement. ``check(trino_sql)``: Trino's own verdict (None: accepted; a message: refused; raises
    ``LookupError`` when it cannot say, e.g. the statement is not a query)."""
    import sqlglot
    from sqlglot import errors, exp

    dialect = SOURCES[source]
    out = {"source_sql": sql, "status": "not_converted", "sql": None, "kind": "", "verified": False, "notes": []}
    flat = " ".join(sql.split())
    bare = re.sub(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*", "", sql, flags=re.S).strip()
    if re.match(r"CREATE\s+(OR\s+(REPLACE|ALTER)\s+)?PROCEDURE\b", bare, re.I):
        converted = convert_procedure(bare, source)
        if converted is not None:
            return converted
    for pattern, kind, instead in ELSEWHERE:
        if re.match(pattern, " ".join(bare.split()), re.I):
            out.update(kind=kind, notes=[{"code": kind, "message": f"no such statement in Cairn: {instead}", "review": True}])
            return out
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect, error_level=errors.ErrorLevel.RAISE) if t is not None]
    except (errors.SqlglotError, RecursionError) as e:
        out["notes"] = [{"code": "parse", "message": f"the statement was not understood: {str(e).splitlines()[0][:300]}", "review": True}]
        return out
    if len(trees) != 1 or isinstance(trees[0], exp.Command):
        out.update(kind="command", notes=[{"code": "command", "message": f"no translation for {flat[:60]!r}", "review": True}])
        return out
    tree, notes = trees[0], []
    out["kind"] = tree.key
    try:
        tree = _rewrite(tree, source, notes)
        try:
            text = tree.sql(dialect="trino", unsupported_level=errors.ErrorLevel.RAISE, pretty=False)
        except errors.UnsupportedError as e:
            _note(notes, "unsupported", f"Trino has no counterpart for part of this: {str(e).splitlines()[0][:300]}", True)
            text = tree.sql(dialect="trino", unsupported_level=errors.ErrorLevel.IGNORE)
    except Exception as e:  # noqa: BLE001 - a statement the translation breaks on is reported, never half-written
        out["notes"] = [{"code": "internal", "message": f"the statement could not be translated ({type(e).__name__})", "review": True}]
        return out
    _leftovers(text, notes)
    out.update(sql=text, notes=notes)
    if check is not None:
        try:
            refused = check(text)
            if refused:
                _note(notes, "trino", f"Trino refused the translation: {refused[:400]}", True)
            else:
                out["verified"] = True
        except LookupError:
            pass
    out["status"] = "needs_review" if any(n["review"] for n in notes) else "converted"
    return out


def convert(sql: str, source: str, check: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    if source not in SOURCES:
        raise ValueError(f"source: one of {', '.join(sorted(SOURCES))}")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL:
        raise ValueError(f"sql: a script of at most {MAX_SQL} characters")
    pieces = split(sql, SOURCES[source])
    if len(pieces) > MAX_STATEMENTS:
        raise ValueError(f"at most {MAX_STATEMENTS} statements at a time")
    statements = [{"index": i + 1, **convert_statement(p, source, check)} for i, p in enumerate(pieces)]
    count = lambda s: sum(1 for x in statements if x["status"] == s)  # noqa: E731
    reasons: dict[str, int] = {}
    for s in statements:
        for n in s["notes"]:
            if n["review"]:
                reasons[n["code"]] = reasons.get(n["code"], 0) + 1
    return {"source": source, "target": "trino", "statements": statements,
            "summary": {"statements": len(statements), "converted": count("converted"), "needs_review": count("needs_review"),
                        "not_converted": count("not_converted"), "verified": sum(1 for s in statements if s["verified"]),
                        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}}


def report(result: dict) -> str:
    """The conversion as a Markdown report: the totals, then every statement that needs a person."""
    s = result["summary"]
    lines = [f"# Conversion from {result['source']}", "",
             f"{s['statements']} statements: {s['converted']} converted ({s['verified']} checked on Trino), "
             f"{s['needs_review']} to review, {s['not_converted']} not converted.", ""]
    if s["reasons"]:
        lines += ["| Reason | Statements |", "|---|---|", *[f"| {k} | {v} |" for k, v in s["reasons"].items()], ""]
    for st in result["statements"]:
        if st["status"] == "converted":
            continue
        first = " ".join(st["source_sql"].split())[:120]
        lines += [f"## {st['index']}. {st['status'].replace('_', ' ')}: `{first}`", ""]
        lines += [f"- {n['message']}" for n in st["notes"]]
        if st["sql"]:
            lines += ["", "```sql", st["sql"], "```"]
        if st.get("procedure"):
            lines += ["", "```sql", *[s + ";" for s in st["procedure"]["statements"]], "```"]
        lines.append("")
    return "\n".join(lines)
