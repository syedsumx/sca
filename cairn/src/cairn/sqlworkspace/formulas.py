"""Spreadsheet formulas for workbooks, compiled to Trino SQL.

A formula is what a spreadsheet user writes: ``[Amount] * 1.2``, ``If([Region] = "EU", "Europe", "Other")``,
``Sum([Amount])``, ``DateTrunc("month", [Ordered at])``. Columns are written in square brackets, text in double
(or single) quotes. Operators: ``+ - * / %``, ``&`` (joins text), comparisons ``= != <> < <= > >=``, ``And``,
``Or``, ``Not``. Functions are only those listed in :data:`FUNCTIONS`; anything else is refused.

Nothing a user writes reaches SQL as written: the formula is parsed into a tree and the SQL is generated from
it. Column names become quoted identifiers (checked against the columns that exist), text becomes escaped
literals, and only the functions and operators below produce SQL. So a formula cannot inject SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

DATE_UNITS = {"second", "minute", "hour", "day", "week", "month", "quarter", "year"}


class FormulaError(Exception):
    def __init__(self, message: str, at: Optional[int] = None):
        super().__init__(message + (f" (at character {at + 1})" if at is not None else ""))
        self.at = at


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# --------------------------------------------------------------------------- tokens
TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<col>\[(?:[^\]]|\]\])+\])
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"]|"")*"|'(?:[^']|'')*')
  | (?P<op><>|!=|<=|>=|[-+*/%&=<>(),])
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.X)


@dataclass
class Tok:
    kind: str
    text: str
    at: int


def tokens(src: str) -> list[Tok]:
    out, i = [], 0
    while i < len(src):
        m = TOKEN.match(src, i)
        if not m:
            raise FormulaError(f"unexpected {src[i]!r}", i)
        if m.lastgroup != "ws":
            out.append(Tok(m.lastgroup, m.group(), i))
        i = m.end()
    out.append(Tok("end", "", len(src)))
    return out


# --------------------------------------------------------------------------- the tree
@dataclass
class Node:
    kind: str                     # num, str, bool, null, col, op, not, neg, call
    value: object = None
    args: tuple = ()
    at: int = 0


PRECEDENCE = {"or": 1, "and": 2, "=": 4, "!=": 4, "<>": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
              "&": 5, "+": 6, "-": 6, "*": 7, "/": 7, "%": 7}


class Parser:
    def __init__(self, src: str):
        if len(src) > 4000:
            raise FormulaError("a formula is at most 4,000 characters")
        self.t = tokens(src)
        self.i = 0
        self.depth = 0

    def peek(self) -> Tok:
        return self.t[self.i]

    def take(self) -> Tok:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, text: str) -> Tok:
        tok = self.take()
        if tok.text != text:
            raise FormulaError(f"expected {text!r}, found {tok.text or 'the end'!r}", tok.at)
        return tok

    def parse(self) -> Node:
        node = self.expr(0)
        if self.peek().kind != "end":
            raise FormulaError(f"unexpected {self.peek().text!r}", self.peek().at)
        return node

    def binop(self) -> Optional[str]:
        tok = self.peek()
        if tok.kind == "op" and tok.text in PRECEDENCE:
            return tok.text
        if tok.kind == "name" and tok.text.lower() in ("and", "or"):
            return tok.text.lower()
        return None

    def expr(self, min_prec: int) -> Node:
        self.depth += 1
        if self.depth > 100:
            raise FormulaError("the formula is nested too deeply")
        left = self.unary()
        while True:
            op = self.binop()
            if op is None or PRECEDENCE[op] < min_prec:
                break
            tok = self.take()
            right = self.expr(PRECEDENCE[op] + 1)
            left = Node("op", op, (left, right), tok.at)
        self.depth -= 1
        return left

    def unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.text == "-":
            self.take()
            return Node("neg", None, (self.unary(),), tok.at)
        if tok.kind == "op" and tok.text == "+":
            self.take()
            return self.unary()
        if tok.kind == "name" and tok.text.lower() == "not":
            self.take()
            return Node("not", None, (self.expr(3),), tok.at)
        return self.atom()

    def atom(self) -> Node:
        tok = self.take()
        if tok.kind == "num":
            return Node("num", tok.text, at=tok.at)
        if tok.kind == "str":
            q = tok.text[0]
            return Node("str", tok.text[1:-1].replace(q + q, q), at=tok.at)
        if tok.kind == "col":
            return Node("col", tok.text[1:-1].replace("]]", "]").strip(), at=tok.at)
        if tok.kind == "op" and tok.text == "(":
            node = self.expr(0)
            self.expect(")")
            return node
        if tok.kind == "name":
            low = tok.text.lower()
            if low in ("true", "false"):
                return Node("bool", low == "true", at=tok.at)
            if low == "null":
                return Node("null", at=tok.at)
            if self.peek().text != "(":
                raise FormulaError(f"{tok.text}: write columns in square brackets, like [{tok.text}]", tok.at)
            self.take()
            args = []
            if self.peek().text != ")":
                while True:
                    args.append(self.expr(0))
                    if self.peek().text == ",":
                        self.take()
                        continue
                    break
            self.expect(")")
            return Node("call", low, tuple(args), tok.at)
        raise FormulaError(f"unexpected {tok.text or 'end of formula'!r}", tok.at)


# --------------------------------------------------------------------------- functions
@dataclass
class Fn:
    arity: tuple[int, int]                     # min, max arguments
    sql: Callable[[list[str], list[Node]], str]
    aggregate: bool = False


def _unit(nodes: list[Node], i: int) -> str:
    n = nodes[i]
    if n.kind != "str" or str(n.value).lower() not in DATE_UNITS:
        raise FormulaError(f"the unit is one of {', '.join(sorted(DATE_UNITS))}, in quotes", n.at)
    return literal(str(n.value).lower())


def _int_literal(nodes: list[Node], i: int, what: str) -> str:
    n = nodes[i]
    if n.kind != "num" or "." in str(n.value):
        raise FormulaError(f"{what} is a whole number", n.at)
    return str(n.value)


FUNCTIONS: dict[str, Fn] = {
    # aggregates
    "sum": Fn((1, 1), lambda a, n: f"sum({a[0]})", True),
    "avg": Fn((1, 1), lambda a, n: f"avg({a[0]})", True),
    "average": Fn((1, 1), lambda a, n: f"avg({a[0]})", True),
    "min": Fn((1, 1), lambda a, n: f"min({a[0]})", True),
    "max": Fn((1, 1), lambda a, n: f"max({a[0]})", True),
    "count": Fn((0, 1), lambda a, n: f"count({a[0]})" if a else "count(*)", True),
    "countdistinct": Fn((1, 1), lambda a, n: f"count(DISTINCT {a[0]})", True),
    "countif": Fn((1, 1), lambda a, n: f"count_if({a[0]})", True),
    "sumif": Fn((2, 2), lambda a, n: f"sum(CASE WHEN {a[1]} THEN {a[0]} END)", True),
    "median": Fn((1, 1), lambda a, n: f"approx_percentile({a[0]}, 0.5)", True),
    "percentile": Fn((2, 2), lambda a, n: f"approx_percentile({a[0]}, {a[1]})", True),
    "stddev": Fn((1, 1), lambda a, n: f"stddev({a[0]})", True),
    # logic
    "if": Fn((2, 3), lambda a, n: f"CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2] if len(a) > 2 else 'NULL'} END"),
    "coalesce": Fn((1, 20), lambda a, n: f"coalesce({', '.join(a)})"),
    "isnull": Fn((1, 1), lambda a, n: f"({a[0]} IS NULL)"),
    "between": Fn((3, 3), lambda a, n: f"({a[0]} BETWEEN {a[1]} AND {a[2]})"),
    "in": Fn((2, 50), lambda a, n: f"({a[0]} IN ({', '.join(a[1:])}))"),
    # numbers
    "abs": Fn((1, 1), lambda a, n: f"abs({a[0]})"),
    "round": Fn((1, 2), lambda a, n: f"round({a[0]}, {_int_literal(n, 1, 'Round’s digits')})" if len(a) > 1 else f"round({a[0]})"),
    "floor": Fn((1, 1), lambda a, n: f"floor({a[0]})"),
    "ceiling": Fn((1, 1), lambda a, n: f"ceiling({a[0]})"),
    "power": Fn((2, 2), lambda a, n: f"power({a[0]}, {a[1]})"),
    "sqrt": Fn((1, 1), lambda a, n: f"sqrt({a[0]})"),
    "ln": Fn((1, 1), lambda a, n: f"ln({a[0]})"),
    "number": Fn((1, 1), lambda a, n: f"try_cast({a[0]} AS double)"),
    # text
    "text": Fn((1, 1), lambda a, n: f"CAST({a[0]} AS varchar)"),
    "upper": Fn((1, 1), lambda a, n: f"upper({a[0]})"),
    "lower": Fn((1, 1), lambda a, n: f"lower({a[0]})"),
    "trim": Fn((1, 1), lambda a, n: f"trim({a[0]})"),
    "len": Fn((1, 1), lambda a, n: f"length({a[0]})"),
    "left": Fn((2, 2), lambda a, n: f"substr({a[0]}, 1, {a[1]})"),
    "right": Fn((2, 2), lambda a, n: f"substr({a[0]}, -({a[1]}))"),
    "mid": Fn((3, 3), lambda a, n: f"substr({a[0]}, {a[1]}, {a[2]})"),
    "contains": Fn((2, 2), lambda a, n: f"(strpos({a[0]}, {a[1]}) > 0)"),
    "startswith": Fn((2, 2), lambda a, n: f"starts_with({a[0]}, {a[1]})"),
    "replace": Fn((3, 3), lambda a, n: f"replace({a[0]}, {a[1]}, {a[2]})"),
    "concat": Fn((1, 20), lambda a, n: "concat(" + ", ".join(f"CAST({x} AS varchar)" for x in a) + ")"),
    # dates
    "today": Fn((0, 0), lambda a, n: "current_date"),
    "now": Fn((0, 0), lambda a, n: "current_timestamp"),
    "date": Fn((1, 1), lambda a, n: f"CAST({a[0]} AS date)"),
    "datetrunc": Fn((2, 2), lambda a, n: f"date_trunc({_unit(n, 0)}, {a[1]})"),
    "datepart": Fn((2, 2), lambda a, n: f"extract({_unit(n, 0).strip(chr(39)).upper()} FROM {a[1]})"),
    "datediff": Fn((3, 3), lambda a, n: f"date_diff({_unit(n, 0)}, {a[1]}, {a[2]})"),
    "dateadd": Fn((3, 3), lambda a, n: f"date_add({_unit(n, 0)}, {a[1]}, {a[2]})"),
    "year": Fn((1, 1), lambda a, n: f"year({a[0]})"),
    "month": Fn((1, 1), lambda a, n: f"month({a[0]})"),
    "day": Fn((1, 1), lambda a, n: f"day({a[0]})"),
}

OPS = {"=": "=", "!=": "<>", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "+": "+", "-": "-", "*": "*",
       "/": "/", "%": "%", "and": "AND", "or": "OR"}


# --------------------------------------------------------------------------- compiling
@dataclass
class Compiled:
    sql: str
    columns: set[str]             # the columns it reads
    aggregate: bool               # it contains an aggregate


class Compiler:
    def __init__(self, columns: dict[str, str]):
        """``columns``: name (as people see it) -> the SQL for it (a quoted column or an expression)."""
        self.columns = {k.lower(): (k, v) for k, v in columns.items()}

    def compile(self, formula: str) -> Compiled:
        tree = Parser(formula).parse()
        used: set[str] = set()
        agg = [False]
        sql = self._sql(tree, used, agg, in_agg=False)
        return Compiled(sql, used, agg[0])

    def _sql(self, n: Node, used: set, agg: list, in_agg: bool) -> str:
        if n.kind == "num":
            return str(n.value)
        if n.kind == "str":
            return literal(str(n.value))
        if n.kind == "bool":
            return "TRUE" if n.value else "FALSE"
        if n.kind == "null":
            return "NULL"
        if n.kind == "col":
            hit = self.columns.get(str(n.value).lower())
            if hit is None:
                raise FormulaError(f"no column [{n.value}]", n.at)
            used.add(hit[0])
            return hit[1]
        if n.kind == "neg":
            return f"(-{self._sql(n.args[0], used, agg, in_agg)})"
        if n.kind == "not":
            return f"(NOT {self._sql(n.args[0], used, agg, in_agg)})"
        if n.kind == "op":
            a, b = (self._sql(x, used, agg, in_agg) for x in n.args)
            if n.value == "&":
                return f"(CAST({a} AS varchar) || CAST({b} AS varchar))"
            if n.value == "/":
                return f"(CAST({a} AS double) / NULLIF({b}, 0))"     # spreadsheet division: no integer division
            return f"({a} {OPS[str(n.value)]} {b})"
        if n.kind == "call":
            fn = FUNCTIONS.get(str(n.value))
            if fn is None:
                raise FormulaError(f"no function {n.value}(); see the formula reference", n.at)
            lo, hi = fn.arity
            if not lo <= len(n.args) <= hi:
                want = f"{lo}" if lo == hi else f"{lo} to {hi}"
                raise FormulaError(f"{n.value}() takes {want} argument{'s' if hi != 1 else ''}", n.at)
            if fn.aggregate:
                if in_agg:
                    raise FormulaError(f"{n.value}() inside another aggregate", n.at)
                agg[0] = True
            args = [self._sql(x, used, agg, in_agg or fn.aggregate) for x in n.args]
            return fn.sql(args, list(n.args))
        raise FormulaError("cannot compile this formula", n.at)
