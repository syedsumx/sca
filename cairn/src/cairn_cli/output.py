"""Writing results: tables and colour only on a terminal (and never with ``--no-color`` or ``NO_COLOR``), CSV, JSON,
JSON Lines and Parquet, row by row as pages arrive so a large result never has to fit in memory."""

from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import os
import sys
from typing import IO, Any, Optional

from cairn_cli.errors import USAGE, CliError

FORMATS = ("table", "csv", "json", "jsonl", "parquet")
TABLE_SAMPLE = 200          # rows read before a table's column widths are fixed
MAX_CELL = 60


class Style:
    def __init__(self, stream: IO = sys.stdout, no_color: bool = False):
        tty = hasattr(stream, "isatty") and stream.isatty()
        self.color = tty and not no_color and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.tty = tty

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def red(self, t: str) -> str:
        return self._c("31", t)

    def green(self, t: str) -> str:
        return self._c("32", t)

    def yellow(self, t: str) -> str:
        return self._c("33", t)

    def cyan(self, t: str) -> str:
        return self._c("36", t)


def err_style(no_color: bool = False) -> Style:
    return Style(sys.stderr, no_color)


def cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def table(headers: list[str], rows: list[list[Any]], style: Optional[Style] = None) -> str:
    """A plain text table (headers bold on a colour terminal)."""
    style = style or Style(io.StringIO())
    texts = [[cell(v).replace("\n", " ") for v in r] for r in rows]
    widths = [min(MAX_CELL, max([len(h)] + [len(r[i]) for r in texts if i < len(r)])) for i, h in enumerate(headers)]

    def fit(s: str, w: int) -> str:
        return (s[: w - 1] + "…") if len(s) > w else s.ljust(w)
    lines = ["  ".join(style.bold(fit(h, w)) for h, w in zip(headers, widths)).rstrip(),
             "  ".join(style.dim("─" * w) for w in widths)]
    lines += ["  ".join(fit(r[i] if i < len(r) else "", w) for i, w in enumerate(widths)).rstrip() for r in texts]
    return "\n".join(lines)


def emit_json(doc: Any, stream: IO = sys.stdout) -> None:
    stream.write(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n")


# ---- result writers (cairn query) -------------------------------------------------------------------------
class Writer:
    def __init__(self, stream: IO):
        self.stream = stream
        self.columns: list[dict] = []
        self.rows = 0

    def start(self, columns: list[dict]) -> None:
        self.columns = columns

    def write(self, rows: list[list[Any]]) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        pass


class CsvWriter(Writer):
    def start(self, columns):
        super().start(columns)
        self.w = csv.writer(self.stream, lineterminator="\n")
        self.w.writerow([c["name"] for c in columns])

    def write(self, rows):
        for r in rows:
            self.w.writerow([cell(v) for v in r])
        self.rows += len(rows)


class JsonLinesWriter(Writer):
    def write(self, rows):
        names = [c["name"] for c in self.columns]
        for r in rows:
            self.stream.write(json.dumps(dict(zip(names, r)), ensure_ascii=False, default=str) + "\n")
        self.rows += len(rows)


class JsonWriter(Writer):
    def start(self, columns):
        super().start(columns)
        self.stream.write("[")

    def write(self, rows):
        names = [c["name"] for c in self.columns]
        for r in rows:
            self.stream.write(("\n  " if self.rows == 0 else ",\n  ")
                              + json.dumps(dict(zip(names, r)), ensure_ascii=False, default=str))
            self.rows += 1

    def finish(self):
        if not self.columns:
            self.stream.write("[")
        self.stream.write("\n]\n" if self.rows else "]\n")


class TableWriter(Writer):
    """Column widths come from the first rows; later rows are cut to them (a table is for reading, not for data)."""

    def __init__(self, stream, style: Style):
        super().__init__(stream)
        self.style = style
        self.buffer: list[list[Any]] = []
        self.widths: Optional[list[int]] = None

    def write(self, rows):
        self.rows += len(rows)
        if self.widths is None:
            self.buffer += rows
            if len(self.buffer) >= TABLE_SAMPLE:
                self._flush()
            return
        self._lines(rows)

    def _flush(self):
        headers = [c["name"] for c in self.columns]
        text = table(headers, self.buffer, self.style)
        self.stream.write(text + "\n")
        self.widths = [min(MAX_CELL, max([len(h)] + [len(cell(r[i]).replace("\n", " ")) for r in self.buffer]))
                       for i, h in enumerate(headers)]
        self.buffer = []

    def _lines(self, rows):
        for r in rows:
            parts = []
            for i, w in enumerate(self.widths or []):
                s = cell(r[i] if i < len(r) else "").replace("\n", " ")
                parts.append((s[: w - 1] + "…") if len(s) > w else s.ljust(w))
            self.stream.write("  ".join(parts).rstrip() + "\n")

    def finish(self):
        if self.widths is None:
            self._flush()


def _arrow_type(pa, trino_type: str):
    t = (trino_type or "").lower()
    base = t.split("(", 1)[0].strip()
    if base in ("bigint",):
        return pa.int64()
    if base in ("integer", "int"):
        return pa.int32()
    if base == "smallint":
        return pa.int16()
    if base == "tinyint":
        return pa.int8()
    if base in ("double",):
        return pa.float64()
    if base in ("real",):
        return pa.float32()
    if base == "boolean":
        return pa.bool_()
    if base == "date":
        return pa.date32()
    if base == "decimal" and "(" in t:
        p, _, s = t.split("(", 1)[1].rstrip(")").partition(",")
        try:
            return pa.decimal128(int(p), int(s or 0))
        except ValueError:
            return pa.string()
    if base == "timestamp" and "with time zone" not in t:
        return pa.timestamp("us")
    return pa.string()


def _convert(pa, typ, v):
    if v is None:
        return None
    if pa.types.is_date32(typ):
        return datetime.date.fromisoformat(str(v)[:10])
    if pa.types.is_timestamp(typ):
        return datetime.datetime.fromisoformat(str(v).replace(" ", "T")[:26])
    if pa.types.is_decimal(typ):
        return decimal.Decimal(str(v))
    if pa.types.is_string(typ):
        return v if isinstance(v, str) else cell(v)
    return v


class ParquetWriter(Writer):
    def __init__(self, stream, path: Optional[str]):
        super().__init__(stream)
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise CliError("--format parquet needs pyarrow: `pip install pyarrow` (or `pipx inject cairn-cli pyarrow`)",
                           USAGE)
        self.pa, self.pq, self.path = pa, pq, path
        self.writer = None

    def start(self, columns):
        super().start(columns)
        pa = self.pa
        self.schema = pa.schema([(c["name"], _arrow_type(pa, c.get("type") or "")) for c in columns])
        sink = self.path if self.path else self.stream.buffer if hasattr(self.stream, "buffer") else self.stream
        self.writer = self.pq.ParquetWriter(sink, self.schema)

    def write(self, rows):
        if not rows:
            return
        pa = self.pa
        arrays = [pa.array([_convert(pa, f.type, r[i]) for r in rows], type=f.type) for i, f in enumerate(self.schema)]
        self.writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
        self.rows += len(rows)

    def finish(self):
        if self.writer is None:
            self.start([])
        self.writer.close()


def writer_for(fmt: str, stream: IO, style: Style, out_path: Optional[str]) -> Writer:
    if fmt == "csv":
        return CsvWriter(stream)
    if fmt == "json":
        return JsonWriter(stream)
    if fmt == "jsonl":
        return JsonLinesWriter(stream)
    if fmt == "parquet":
        if out_path is None and hasattr(stream, "isatty") and stream.isatty():
            raise CliError("--format parquet writes binary: give --out FILE or redirect standard output", USAGE)
        return ParquetWriter(stream, out_path)
    if fmt == "table":
        return TableWriter(stream, style)
    raise CliError(f"--format: one of {', '.join(FORMATS)}", USAGE)
