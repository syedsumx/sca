"""Oracle as a mirror source: an Oracle table kept in a lakehouse table through LogMiner.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``oracle://`` (or
``oracle+oracledb://``): ``oracle://user:password@host:1521/service``, read with python-oracledb in thin mode (no
Oracle client to install). Cairn changes nothing on the server; a database administrator prepares it:

* the database runs in ``ARCHIVELOG`` mode (LogMiner reads archived logs once the online ones have been reused, so
  a paused mirror can carry on), with minimal supplemental logging (``ALTER DATABASE ADD SUPPLEMENTAL LOG DATA``);
* the table has a primary key and table-level supplemental logging: ``ALTER TABLE s.t ADD SUPPLEMENTAL LOG DATA
  (ALL) COLUMNS`` (every change then carries the whole row) or ``(PRIMARY KEY) COLUMNS`` (an update carries its key
  and the columns it changed; the rest are read back by key);
* the connection's user has ``LOGMINING``, ``SELECT ANY TRANSACTION``, ``EXECUTE`` on ``SYS.DBMS_LOGMNR``,
  ``SELECT`` on ``V_$LOGMNR_CONTENTS``, ``V_$LOG``, ``V_$LOGFILE``, ``V_$ARCHIVED_LOG``, ``V_$DATABASE`` and
  ``V_$TRANSACTION``, and ``SELECT`` and ``FLASHBACK`` on the table (an owner has both).

A missing setting or grant is refused when the mirror is made, with the statements that would fix it.

**The copy** is a flashback query (``SELECT ... AS OF SCN``) at the SCN read first, so it is exactly the table at
that SCN, and the changes that follow are those of transactions committed after it.

**Changes** come from LogMiner (``DBMS_LOGMNR.START_LOGMNR`` with ``DICT_FROM_ONLINE_CATALOG`` and
``COMMITTED_DATA_ONLY``), between where the mirror had read to and the current SCN. In a pluggable database
LogMiner finds the logs that cover the range itself (adding log files is the root's business there); in a
non-CDB, or the root, Cairn adds the archived and online logs that cover it. Column values are read with
``DBMS_LOGMNR.MINE_VALUE`` (a structured read of each change's redo and undo values, typed by the column's type),
not parsed out of SQL text. LogMiner reads with today's dictionary, so a change logged before the table was
altered (a column added since) cannot be read that way ("Dictionary Mismatch"): its ``SQL_REDO`` then holds the
values in Oracle's internal formats (``HEXTORAW('c102')``) under positional names (``"COL 1"``), and those
statements are parsed with a real tokenizer (:func:`tokenize`, :func:`parse_dml`) and the values decoded
(:func:`internal`). LOB columns (CLOB, NCLOB, BLOB) are never in the redo values: an insert or update reads them
back by key as of the transaction's commit SCN (flashback query; the current row if the undo is gone).

Transactions are applied whole, in commit order. A transaction that changes a key deletes the old row; a column
added at the source is added to the lakehouse table; ``TRUNCATE`` empties it; any other DDL on the table fails the
mirror and says so. Offsets are commit SCNs, and every lakehouse commit records the next one, so a crash between
the commit and the mirror's own record replays changes that are then skipped.

**Position.** A mirror records two SCNs: the commit SCN it has applied to, and the SCN to restart mining from (the
start of the oldest transaction still open when it last read, so a long transaction that commits later is read
whole). Oracle keeps no slot for a mirror: archived logs must outlast a paused one. If the logs it needs are gone,
the mirror fails and says to re-create it.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import struct
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

MAX_ROWS = 20_000
URL = re.compile(r"^oracle(\+\w+)?://")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$")
# An open transaction older than this makes the mirror "holding": it keeps mining from that transaction's start (so
# its changes are not lost when it commits) and says which transaction it waits on.
HOLD_AFTER = float(os.environ.get("CAIRN_ORACLE_MIRROR_HOLD_SECONDS", "3600"))
LOB_OPS = ("SEL_LOB_LOCATOR", "LOB_WRITE", "LOB_TRIM", "LOB_ERASE")
GONE = ("the archived logs this mirror had read to are gone from the server: delete the mirror and create it "
        "again to copy the table afresh")
GONE_CODES = ("ORA-01291", "ORA-00308", "ORA-01284", "ORA-01292", "ORA-01371")
FLASHBACK_GONE = ("ORA-01555", "ORA-08180", "ORA-08181", "ORA-01466", "ORA-30052")
NLS = ["ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'",
       "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
       "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM'",
       "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'",
       "ALTER SESSION SET TIME_ZONE = 'UTC'"]
UTC = dt.timezone.utc

# Oracle types and how they arrive: kind -> Python type.
KINDS = {"FLOAT": "float", "BINARY_FLOAT": "float", "BINARY_DOUBLE": "float", "DATE": "date",
         "VARCHAR2": "str", "CHAR": "str", "NVARCHAR2": "nstr", "NCHAR": "nstr", "RAW": "raw", "CLOB": "lob",
         "NCLOB": "lob", "BLOB": "blob", "ROWID": "text", "UROWID": "text"}
LOBS = ("lob", "blob")
CHARSETS = {"AL32UTF8": "utf-8", "UTF8": "utf-8", "AL16UTF16": "utf-16-be", "WE8MSWIN1252": "cp1252",
            "WE8ISO8859P1": "latin-1", "WE8ISO8859P15": "iso8859-15", "US7ASCII": "ascii"}


class ParseError(ValueError):
    pass


@dataclass
class Col:
    name: str               # as Oracle has it (case kept)
    out: str                # the lakehouse column: lowercase
    type: str               # DATA_TYPE
    kind: str               # int dec numtext float date ts tstz tsltz str nstr raw lob blob text
    seg: int                # SEGMENT_COLUMN_ID: the n of "COL n" in a statement LogMiner could not map
    precision: Optional[int] = None    # a dec column's decimal(precision, scale)
    scale: int = 0
    default: Optional[str] = None      # DATA_DEFAULT, when the column has one


@dataclass
class Desc:
    owner: str
    table: str
    columns: list[Col]
    key: list[str]          # lakehouse names
    all_columns: bool       # ALL COLUMNS supplemental logging: every change carries the whole row
    charset: str = "AL32UTF8"
    ncharset: str = "AL16UTF16"
    pdb: bool = True
    dbtz: Optional[dt.timedelta] = dt.timedelta(0)   # DBTIMEZONE as an offset (None: a region)
    by_out: dict = field(default_factory=dict)

    def __post_init__(self):
        self.by_out = {c.out: c for c in self.columns}

    @property
    def qualified(self) -> str:
        return f'"{self.owner}"."{self.table}"'


# ---- connections ------------------------------------------------------------------------------
def settings(url: str) -> dict:
    u = urlsplit(URL.sub("oracle://", url))
    if u.scheme != "oracle":
        raise ValueError("not an oracle:// URL")
    service = (u.path or "/").lstrip("/")
    if not service:
        raise ValueError("an oracle:// URL names its service: oracle://user:password@host:1521/service")
    return {"host": u.hostname or "localhost", "port": u.port or 1521, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "service_name": service}


def _lobs_inline(cursor, metadata):
    """CLOBs as text and BLOBs as bytes, fetched with the row."""
    import oracledb

    if metadata.type_code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def connect(url: str):
    import oracledb

    s = settings(url)
    conn = oracledb.connect(user=s["user"], password=s["password"], host=s["host"], port=s["port"],
                            service_name=s["service_name"], tcp_connect_timeout=10)
    conn.call_timeout = 600_000
    conn.outputtypehandler = _lobs_inline
    with conn.cursor() as cur:
        for stmt in NLS:                                  # the text forms typed() reads
            cur.execute(stmt)
    return conn


def _code(e: BaseException) -> str:
    m = re.search(r"ORA-\d{5}", str(e))
    return m.group(0) if m else ""


def _gone(e: BaseException) -> Optional[str]:
    """The code of a log LogMiner could not find, wherever it is in the error stack (ORA-00604 wraps it)."""
    return next((c for c in GONE_CODES if c in str(e)), None)


def arrow_schema(desc: Desc):
    """The lakehouse table's columns, typed from Oracle's (a column that is NULL in every row copied keeps its type)."""
    import pyarrow as pa

    types = {"int": pa.int64(), "float": pa.float64(), "date": pa.timestamp("us"), "ts": pa.timestamp("us"),
             "tstz": pa.timestamp("us", tz="UTC"), "tsltz": pa.timestamp("us", tz="UTC")}
    return pa.schema([pa.field(c.out, pa.decimal128(c.precision or 38, c.scale) if c.kind == "dec"
                               else types.get(c.kind, pa.string())) for c in desc.columns])


# ---- what a table needs -----------------------------------------------------------------------
def number_type(precision: Optional[int], scale: Optional[int]) -> tuple[str, Optional[int], int]:
    """How a NUMBER column lands, exactly: ``(kind, precision, scale)``. NUMBER(p,0) with p up to 18 is a 64-bit
    integer; any other NUMBER with a precision (or INTEGER, NUMBER(*,0)) a decimal(p,s) (Iceberg holds up to 38
    digits); NUMBER with neither precision nor scale can hold any value (1e125, 1/3), so its exact text."""
    if precision is None and scale is None:
        return "numtext", None, 0
    s = scale or 0
    p = precision if precision is not None else 38
    if s < 0:                                             # NUMBER(5,-2): whole numbers of up to p - s digits
        p, s = min(38, p - s), 0
    if s == 0 and p <= 18:
        return "int", None, 0
    return "dec", min(p, 38), min(s, 38)


def _kind(data_type: str, scale: Optional[int]) -> Optional[str]:
    t = re.sub(r"\(\d+\)", "", data_type)
    if t == "NUMBER":
        return "number"
    if t == "TIMESTAMP":
        return "ts"
    if t == "TIMESTAMP WITH TIME ZONE":
        return "tstz"
    if t == "TIMESTAMP WITH LOCAL TIME ZONE":
        return "tsltz"
    if t.startswith("INTERVAL"):
        return "text"
    return KINDS.get(t)


def _probe(cur, sql: str, **binds) -> Optional[str]:
    """None if the statement runs, else the Oracle error code."""
    try:
        cur.execute(sql, binds)
        cur.fetchall()
        return None
    except Exception as e:  # noqa: BLE001 - the code says what is missing
        return _code(e) or type(e).__name__


def privileges(conn, owner: str, table: str) -> list[str]:
    """What the connection's user lacks, as the GRANT statements a DBA would run."""
    missing = []
    with conn.cursor() as cur:
        cur.execute("SELECT USER FROM dual")
        user = cur.fetchone()[0]
        cur.execute("SELECT privilege FROM session_privs")
        have = {r[0] for r in cur.fetchall()}
        for p in ("LOGMINING", "SELECT ANY TRANSACTION"):
            if p not in have:
                missing.append(f"GRANT {p} TO {user}")
        cur.execute("SELECT COUNT(*) FROM all_objects WHERE owner = 'SYS' AND object_name = 'DBMS_LOGMNR' "
                    "AND object_type = 'PACKAGE'")
        if not cur.fetchone()[0]:
            missing.append(f"GRANT EXECUTE ON SYS.DBMS_LOGMNR TO {user}")
        for view in ("V$DATABASE", "V$LOG", "V$LOGFILE", "V$ARCHIVED_LOG", "V$TRANSACTION"):
            if _probe(cur, f"SELECT 1 FROM {view} WHERE ROWNUM < 1") in ("ORA-00942", "ORA-01031"):
                missing.append(f"GRANT SELECT ON SYS.{view.replace('V$', 'V_$')} TO {user}")
        # Outside a LogMiner session the view answers ORA-01306: readable. ORA-00942: not granted.
        if _probe(cur, "SELECT 1 FROM v$logmnr_contents WHERE ROWNUM < 1") in ("ORA-00942", "ORA-01031"):
            missing.append(f"GRANT SELECT ON SYS.V_$LOGMNR_CONTENTS TO {user}")
        name = f'"{owner}"."{table}"'
        if _probe(cur, f"SELECT 1 FROM {name} WHERE ROWNUM < 1"):
            missing.append(f"GRANT SELECT ON {name} TO {user}")
        if owner != user and "FLASHBACK ANY TABLE" not in have:
            # The copy is a flashback query: its owner, FLASHBACK ANY TABLE, or FLASHBACK on it (directly, to
            # PUBLIC, or through a role).
            cur.execute("SELECT (SELECT COUNT(*) FROM all_tab_privs WHERE table_schema = :o AND table_name = :t "
                        "AND privilege = 'FLASHBACK' AND grantee IN (USER, 'PUBLIC')) + (SELECT COUNT(*) FROM "
                        "role_tab_privs WHERE owner = :o AND table_name = :t AND privilege = 'FLASHBACK') FROM dual",
                        o=owner, t=table)
            if not cur.fetchone()[0]:
                missing.append(f"GRANT FLASHBACK ON {name} TO {user}")
    return missing


def _resolve(cur, owner: str, table: str) -> tuple[str, str]:
    """The names as the dictionary has them: as given, else in upper case (Oracle's unquoted names)."""
    for o, t in ((owner, table), (owner.upper(), table.upper())):
        cur.execute("SELECT owner, table_name FROM all_tables WHERE owner = :o AND table_name = :t", o=o, t=t)
        row = cur.fetchone()
        if row:
            return row[0], row[1]
    raise ValueError(f"{owner}.{table} does not exist, or the connection's user cannot see it (GRANT SELECT on it)")


def check(conn, owner: str, table: str) -> Desc:
    """The table as a mirror reads it, after checking the server, the table and the user's grants; a refusal is a
    ValueError that says what to run."""
    for part in (owner, table):
        if not IDENT.match(part):
            raise ValueError(f"{part!r} is not a plain Oracle name")
    with conn.cursor() as cur:
        owner, table = _resolve(cur, owner, table)
        missing = privileges(conn, owner, table)
        if missing:
            raise ValueError("the connection's user lacks what a mirror needs; a DBA runs: " + "; ".join(missing))
        cur.execute("SELECT column_name, data_type, data_precision, data_scale, segment_column_id, data_type_owner, "
                    "data_default FROM all_tab_cols WHERE owner = :o AND table_name = :t AND hidden_column = 'NO' "
                    "AND virtual_column = 'NO' ORDER BY column_id", o=owner, t=table)
        cols, bad = [], []
        for name, data_type, precision, scale, seg, type_owner, default in cur.fetchall():
            kind = None if type_owner else _kind(data_type, scale)
            if kind is None or not IDENT.match(name):
                bad.append(f"{name} ({data_type})")
                continue
            p, s = None, 0
            if kind == "number":
                kind, p, s = number_type(None if precision is None else int(precision),
                                         None if scale is None else int(scale))
            default = (default or "").strip() or None
            if default and default.upper() == "NULL":
                default = None
            cols.append(Col(name, name.lower(), data_type, kind, int(seg or 0), p, s, default))
        if bad:
            raise ValueError(f"{owner}.{table} has columns a mirror cannot read: {', '.join(bad)} (LONG, LONG RAW, "
                             "XMLTYPE, BOOLEAN, JSON, VECTOR, BFILE and object types are not mirrored)")
        outs = [c.out for c in cols]
        if len(set(outs)) != len(outs):
            raise ValueError(f"{owner}.{table} has columns whose names differ only in case")
        cur.execute("SELECT cc.column_name FROM all_constraints c JOIN all_cons_columns cc "
                    "ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
                    "WHERE c.owner = :o AND c.table_name = :t AND c.constraint_type = 'P' ORDER BY cc.position",
                    o=owner, t=table)
        key = [r[0].lower() for r in cur.fetchall()]
        if not key:
            raise ValueError(f"{owner}.{table} has no primary key: a mirror applies changes by key")
        fine = [c.name for c in cols if c.out in key and c.kind in ("ts", "tstz", "tsltz")
                and int((re.search(r"\((\d+)\)", c.type) or [0, 6])[1]) > 6]
        if fine:
            raise ValueError(f"{owner}.{table} has a primary key on {', '.join(fine)}, a TIMESTAMP finer than "
                             "microseconds: the lakehouse keeps microseconds, so two keys could become one")
        cur.execute("SELECT log_mode, supplemental_log_data_min, supplemental_log_data_pk, supplemental_log_data_all, "
                    "force_logging FROM v$database")
        log_mode, sup_min, sup_pk, sup_all, force = cur.fetchone()
        if log_mode != "ARCHIVELOG":
            raise ValueError("the database runs in NOARCHIVELOG mode: LogMiner needs the archived logs to carry a "
                             "mirror over log switches. A DBA turns it on: SHUTDOWN IMMEDIATE; STARTUP MOUNT; "
                             "ALTER DATABASE ARCHIVELOG; ALTER DATABASE OPEN")
        cur.execute("SELECT log_group_type FROM all_log_groups WHERE owner = :o AND table_name = :t",
                    o=owner, t=table)
        groups = {r[0] for r in cur.fetchall()}
        all_columns = sup_all == "YES" or "ALL COLUMN LOGGING" in groups
        table_ok = all_columns or sup_pk == "YES" or "PRIMARY KEY LOGGING" in groups
        statements = []
        if sup_min not in ("YES", "IMPLICIT"):
            statements.append("ALTER DATABASE ADD SUPPLEMENTAL LOG DATA")
        if not table_ok:
            statements.append(f'ALTER TABLE "{owner}"."{table}" ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS')
        if statements:
            raise ValueError("supplemental logging is off, so the redo log does not say which row a change is to. "
                             "A DBA runs: " + "; ".join(statements)
                             + " ((PRIMARY KEY) COLUMNS also works: the rest of an updated row is then read back)")
        if force != "YES":
            # A NOLOGGING table (or partition, or LOB) lets direct-path loads skip the redo: LogMiner never sees them.
            q = f'"{owner}"."{table}"'
            fixes = []
            cur.execute("SELECT logging FROM all_tables WHERE owner = :o AND table_name = :t", o=owner, t=table)
            if cur.fetchone()[0] == "NO":
                fixes.append(f"ALTER TABLE {q} LOGGING")
            cur.execute("SELECT partition_name FROM all_tab_partitions WHERE table_owner = :o AND table_name = :t "
                        "AND logging = 'NO'", o=owner, t=table)
            fixes += [f'ALTER TABLE {q} MODIFY PARTITION "{r[0]}" LOGGING' for r in cur.fetchall()]
            cur.execute("SELECT column_name FROM all_lobs WHERE owner = :o AND table_name = :t AND logging = 'NO'",
                        o=owner, t=table)
            fixes += [f'ALTER TABLE {q} MODIFY LOB ("{r[0]}") (NOCACHE LOGGING)' for r in cur.fetchall()]
            if fixes:
                raise ValueError(f"{owner}.{table} is NOLOGGING, so direct-path loads into it leave no redo for a "
                                 "mirror to read. A DBA runs: ALTER DATABASE FORCE LOGGING (or: "
                                 + "; ".join(fixes) + ")")
        cur.execute("SELECT parameter, value FROM nls_database_parameters "
                    "WHERE parameter IN ('NLS_CHARACTERSET', 'NLS_NCHAR_CHARACTERSET')")
        nls = dict(cur.fetchall())
        cur.execute("SELECT SYS_CONTEXT('USERENV', 'CON_ID'), DBTIMEZONE FROM dual")
        con, dbtz = cur.fetchone()
        pdb = int(con or 0) > 1
    return Desc(owner, table, cols, key, all_columns, nls.get("NLS_CHARACTERSET", "AL32UTF8"),
                nls.get("NLS_NCHAR_CHARACTERSET", "AL16UTF16"), pdb, tz_offset(dbtz))


def tz_offset(text: Optional[str]) -> Optional[dt.timedelta]:
    """``+05:30`` as a timedelta; None for a region name (``Europe/Paris``)."""
    m = re.match(r"^([+-])(\d\d):(\d\d)$", (text or "").strip())
    if not m:
        return None
    off = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return -off if m.group(1) == "-" else off


# ---- values ---------------------------------------------------------------------------------
def _ts(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S")


def typed(col: Col, text: Any) -> Any:
    """A value in the text form the session's NLS settings give (TO_CHAR, MINE_VALUE) as the Python value written
    to the lakehouse: NUMBER with scale 0 an int, other numbers floats, DATE and TIMESTAMP naive datetimes,
    TIMESTAMP WITH (LOCAL) TIME ZONE UTC datetimes, RAW and BLOB hex, the rest text."""
    if text is None:
        return None
    k = col.kind
    if k == "blob":
        return bytes(text).hex()
    if not isinstance(text, str):
        return text
    if k == "int":
        return int(Decimal(text))
    if k == "dec":
        return _dec(col, Decimal(text))
    if k == "numtext":
        return num_text(Decimal(text))
    if k == "float":
        return float(text) if col.type.startswith("BINARY") else float(Decimal(text))
    if k == "date":
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if k == "ts":
        return _ts(text)
    if k in ("tstz", "tsltz"):
        m = re.match(r"^(\S+ \S+)(?: ([+-]\d\d):?(\d\d))?$", text.strip())
        if not m:
            raise ParseError(f"{col.name}: {text!r} is not a timestamp")
        value = _ts(m.group(1))
        if m.group(2) is None:                            # local time zone: shown in the session's, UTC
            return value.replace(tzinfo=UTC)
        sign = -1 if m.group(2).startswith("-") else 1
        off = dt.timedelta(hours=int(m.group(2)[1:]), minutes=int(m.group(3)))
        return value.replace(tzinfo=dt.timezone(sign * off)).astimezone(UTC)
    if k == "raw":
        return text.lower()
    return text


def _dec(col: Col, value: Decimal) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-col.scale)) if value.is_finite() else value


def num_text(value: Decimal) -> str:
    """A NUMBER without precision or scale, as exact text: the same digits however it was read (TO_CHAR gives
    ``.5`` and ``1.0E+40``, the redo's internal format neither)."""
    if not value.is_finite():
        return str(value)
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def oracle_number(b: bytes) -> Decimal:
    """Oracle's internal NUMBER: an exponent byte, then base-100 digits (negatives inverted, ended by 102)."""
    if not b:
        raise ParseError("an empty NUMBER")
    if b == b"\x80":
        return Decimal(0)
    if b[0] & 0x80:
        exp, digits, sign = b[0] - 0xC1, [x - 1 for x in b[1:]], 1
    else:
        body = b[1:-1] if b[-1:] == b"\x66" else b[1:]
        exp, digits, sign = (~b[0] & 0xFF) - 0xC1, [101 - x for x in body], -1
    value = sum((Decimal(d).scaleb(2 * (exp - i)) for i, d in enumerate(digits)), Decimal(0))
    return value if sign > 0 else -value


def _binary_float(b: bytes) -> float:
    b = bytes([b[0] & 0x7F]) + b[1:] if b[0] & 0x80 else bytes(~x & 0xFF for x in b)
    return struct.unpack(">f" if len(b) == 4 else ">d", b)[0]


def _datetime(b: bytes) -> dt.datetime:
    if b[0] < 100:
        raise ParseError("a date before the common era")
    nanos = int.from_bytes(b[7:11], "big") if len(b) >= 11 else 0
    return dt.datetime((b[0] - 100) * 100 + b[1] - 100, b[2], b[3], b[4] - 1, b[5] - 1, b[6] - 1, nanos // 1000)


def internal(col: Col, raw: bytes, desc: Desc) -> Any:
    """A value in Oracle's internal format, as LogMiner shows it (``HEXTORAW``) for a change it could not map with
    today's dictionary."""
    k = col.kind
    if k in ("raw", "blob"):
        return raw.hex()
    if not raw:
        return None
    if k in ("int", "float", "dec", "numtext"):
        if col.type.startswith("BINARY"):
            return _binary_float(raw)
        n = oracle_number(raw)
        return {"int": int, "float": float, "numtext": num_text}.get(k, lambda v: _dec(col, v))(n)
    if k in ("date", "ts"):
        return _datetime(raw)
    if k == "tstz":
        return _datetime(raw[:11]).replace(tzinfo=UTC)    # kept in UTC, the zone after it
    if k == "tsltz":                                      # kept in the database's time zone
        if desc.dbtz is None:
            raise ParseError(f"{col.name}: the database's time zone is a region, not an offset")
        return (_datetime(raw) - desc.dbtz).replace(tzinfo=UTC)
    if k in ("str", "nstr", "lob"):
        charset = desc.ncharset if k == "nstr" else desc.charset
        if charset not in CHARSETS:
            raise ParseError(f"{col.name}: the {charset} character set is not decoded")
        return raw.decode(CHARSETS[charset])
    raise ParseError(f"{col.name}: a {col.type} value logged before the table was altered cannot be decoded")


# ---- SQL_REDO, for changes LogMiner could not map --------------------------------------------
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


def tokenize(text: str) -> list[tuple[str, str]]:
    """``(kind, value)`` tokens: ``id`` (a quoted identifier, quotes removed, ``""`` as ``"``), ``word`` (upper
    case), ``str`` (a string literal, ``''`` as ``'``), ``num`` and ``p`` (punctuation)."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "'\"":
            buf, j = [], i + 1
            while True:
                if j >= n:
                    raise ParseError(f"an unterminated {'string' if ch == chr(39) else 'name'} at {i}")
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        buf.append(ch)
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            out.append(("str" if ch == "'" else "id", "".join(buf)))
            i = j + 1
            continue
        if ch.isdigit() or (ch in "+-." and i + 1 < n and (text[i + 1].isdigit() or text[i + 1] == ".")
                            and (not out or out[-1][0] == "p" or out[-1] == ("word", "VALUES"))):
            m = NUMBER.match(text, i)
            if m:
                out.append(("num", m.group(0)))
                i = m.end()
                continue
        m = WORD.match(text, i)
        if m:
            out.append(("word", m.group(0).upper()))
            i = m.end()
            continue
        if ch in "(),=;.*<>!|+-/%:@":
            out.append(("p", ch))
            i += 1
            continue
        raise ParseError(f"unexpected {ch!r} at {i}")
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t, self.i = tokens, 0

    def peek(self, ahead: int = 0) -> tuple[str, str]:
        j = self.i + ahead
        return self.t[j] if j < len(self.t) else ("eof", "")

    def take(self, kind: str, value: Optional[str] = None) -> str:
        tok = self.peek()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise ParseError(f"expected {value or kind}, found {tok[1] or 'the end'!r}")
        self.i += 1
        return tok[1]

    def name(self) -> str:
        tok = self.peek()
        if tok[0] not in ("id", "word"):
            raise ParseError(f"expected a name, found {tok[1]!r}")
        self.i += 1
        return tok[1]

    def qualified(self) -> list[str]:
        parts = [self.name()]
        while self.peek() == ("p", "."):
            self.i += 1
            parts.append(self.name())
        return parts

    def expr(self) -> tuple:
        kind, value = self.peek()
        if (kind, value) == ("word", "NULL"):
            self.i += 1
            return ("null",)
        if kind in ("str", "num"):
            self.i += 1
            return (kind, value)
        if kind == "word" and self.peek(1) == ("p", "("):
            self.i += 2
            args = []
            if self.peek() != ("p", ")"):
                args.append(self.expr())
                while self.peek() == ("p", ","):
                    self.i += 1
                    args.append(self.expr())
            self.take("p", ")")
            return ("call", value, args)
        raise ParseError(f"unexpected {value or 'end'!r} where a value belongs")

    def where(self) -> dict:
        out: dict = {}
        if self.peek() != ("word", "WHERE"):
            return out
        self.i += 1
        while True:
            col = self.name()
            if self.peek() == ("word", "IS"):
                self.i += 1
                self.take("word", "NULL")
                out[col] = ("null",)
            else:
                self.take("p", "=")
                out[col] = self.expr()
            if self.peek() != ("word", "AND"):
                return out
            self.i += 1

    def end(self) -> None:
        if self.peek() == ("p", ";"):
            self.i += 1
        if self.peek()[0] != "eof":
            raise ParseError(f"unexpected {self.peek()[1]!r} after the statement")


def parse_dml(text: str) -> tuple[str, list[str], dict, dict]:
    """A LogMiner ``SQL_REDO`` insert, update or delete as ``(op, table, new values, old values)``: values are
    ``("null",)``, ``("str", s)``, ``("num", s)`` or ``("call", NAME, [args])``; old values are the ``WHERE``
    clause's (``IS NULL`` as null), ``ROWID`` left out."""
    p = _Parser(tokenize(text))
    verb = p.take("word")
    new: dict = {}
    if verb == "INSERT":
        p.take("word", "INTO")
        table = p.qualified()
        p.take("p", "(")
        cols = [p.name()]
        while p.peek() == ("p", ","):
            p.i += 1
            cols.append(p.name())
        p.take("p", ")")
        p.take("word", "VALUES")
        p.take("p", "(")
        values = [p.expr()]
        while p.peek() == ("p", ","):
            p.i += 1
            values.append(p.expr())
        p.take("p", ")")
        if len(cols) != len(values):
            raise ParseError(f"{len(cols)} columns and {len(values)} values")
        new = dict(zip(cols, values))
        old: dict = {}
    elif verb == "UPDATE":
        table = p.qualified()
        p.take("word", "SET")
        while True:
            col = p.name()
            p.take("p", "=")
            new[col] = p.expr()
            if p.peek() != ("p", ","):
                break
            p.i += 1
        old = p.where()
    elif verb == "DELETE":
        p.take("word", "FROM")
        table = p.qualified()
        old = p.where()
    else:
        raise ParseError(f"not an insert, update or delete: {verb}")
    p.end()
    old.pop("ROWID", None)
    return verb.lower(), table, new, old


def ddl_effect(text: str) -> str:
    """What a DDL statement on the mirrored table means for it: ``truncate``, ``add`` (a column added),
    ``none`` (nothing a mirror sees: grants, comments, indexes, supplemental logging, storage) or ``fail``."""
    try:
        tokens = tokenize(text)
    except ParseError:
        return "fail"
    words = [v for k, v in tokens if k == "word"]
    if not words:
        return "fail"
    if words[0] == "TRUNCATE":
        return "truncate"
    if words[0] in ("GRANT", "REVOKE", "COMMENT", "ANALYZE", "AUDIT", "NOAUDIT", "CREATE"):
        return "none" if words[0] != "CREATE" or "INDEX" in words[:4] else "fail"
    if words[:2] != ["ALTER", "TABLE"]:
        return "fail"
    # ALTER TABLE <name> <clause>: the clause starts after the (possibly qualified) table name.
    j = 2
    j += 1
    while j < len(tokens) and tokens[j] == ("p", "."):
        j += 2
    clause = [v for k, v in tokens[j:] if k in ("word", "p")]
    first = clause[0] if clause else ""
    if first == "ADD":
        nxt = clause[1] if len(clause) > 1 else ""
        if nxt == "(":
            nxt = clause[2] if len(clause) > 2 else ""
        if nxt in ("SUPPLEMENTAL", "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "PARTITION",
                   "SUBPARTITION"):
            return "none"
        return "add"
    if first == "DROP":                                   # a constraint goes: nothing; anything else (a column,
        return "none" if len(clause) > 1 and clause[1] in ("CONSTRAINT", "UNIQUE") else "fail"   # partitions' rows)
    if first in ("LOGGING", "NOLOGGING", "ENABLE", "DISABLE", "PARALLEL", "NOPARALLEL", "CACHE", "NOCACHE",
                 "STORAGE", "PCTFREE", "PCTUSED", "INITRANS", "COMPRESS", "NOCOMPRESS", "MOVE", "SHRINK",
                 "ALLOCATE", "DEALLOCATE", "MONITORING", "NOMONITORING", "READ", "INMEMORY", "NO"):
        return "none"
    return "fail"


def unistr(text: str) -> str:
    """Oracle's UNISTR: ``\\XXXX`` is a UTF-16 code unit (surrogate pairs joined), ``\\\\`` a backslash."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
        elif text[i + 1:i + 2] == "\\":
            out.append("\\")
            i += 2
        elif re.fullmatch(r"[0-9A-Fa-f]{4}", text[i + 1:i + 5]):
            out.append(chr(int(text[i + 1:i + 5], 16)))
            i += 5
        else:
            raise ParseError(f"UNISTR: a backslash not followed by four hex digits or another backslash at {i}")
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")


def _value(col: Col, v: tuple, desc: Desc) -> Any:
    kind = v[0]
    if kind == "null":
        return None
    if kind in ("str", "num"):
        return typed(col, v[1])
    name, args = v[1], v[2]
    if name == "HEXTORAW" and len(args) == 1 and args[0][0] == "str":
        return internal(col, bytes.fromhex(args[0][1]), desc)
    if name in ("TO_DATE", "TO_TIMESTAMP", "TO_TIMESTAMP_TZ") and args and args[0][0] == "str":
        return typed(col, args[0][1])                     # the session's NLS formats: see NLS
    if name == "UNISTR" and len(args) == 1 and args[0][0] == "str":
        return unistr(args[0][1])
    raise ParseError(f"{col.name}: a value LogMiner wrote as {name}(...) is not read")


def statement_values(desc: Desc, values: dict) -> dict:
    """A parsed statement's values by lakehouse column; ``"COL n"`` names map by segment column id, LOB columns
    are left out (they are read back)."""
    by_name = {c.name: c for c in desc.columns}
    by_seg = {f"COL {c.seg}": c for c in desc.columns}
    out = {}
    for name, v in values.items():
        col = by_name.get(name) or by_seg.get(name)
        if col is None:
            raise ParseError(f"a change names {name}, which {desc.owner}.{desc.table} does not have now: it was "
                             "logged before a change to the table other than a column added")
        if col.kind in LOBS:
            continue
        out[col.out] = _value(col, v, desc)
    return out


# ---- position, copy, re-reads -----------------------------------------------------------------
def _oldest_open(cur) -> Optional[dict]:
    """The open transaction that started first: its start SCN, XID, age in seconds and (with SELECT on V_$SESSION)
    its session."""
    cur.execute("SELECT * FROM (SELECT start_scn, xidusn || '.' || xidslot || '.' || xidsqn, "
                "ROUND((SYSDATE - start_date) * 86400), ses_addr FROM v$transaction ORDER BY start_scn) "
                "WHERE ROWNUM = 1")
    row = cur.fetchone()
    if row is None:
        return None
    out = {"scn": int(row[0]), "xid": row[1], "age": int(row[2] or 0), "session": None}
    try:
        cur.execute("SELECT sid || ',' || serial# || ' ' || username || ' ' || program FROM v$session "
                    "WHERE saddr = :a", a=row[3])
        got = cur.fetchone()
        out["session"] = got[0] if got else None
    except Exception:  # noqa: BLE001 - V$SESSION is optional: the XID says which transaction
        pass
    return out


def scan_position(conn) -> tuple[int, int, Optional[dict]]:
    """``(current SCN, SCN to mine from, the oldest open transaction)``.

    The order of the three reads is what makes the second safe. ``before`` is read first, then the open
    transactions, then ``end``. A transaction that commits after ``end`` either was open when the transactions were
    read (so the restart point is at or before its start), or started after that read, when the SCN was already at
    least ``before`` (so its start is at or after ``before``). Taking the minimum of the two can miss neither; reading
    ``end`` before the transactions could miss one that started between the two reads."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_scn FROM v$database")
        before = int(cur.fetchone()[0])
        oldest = _oldest_open(cur)
        cur.execute("SELECT current_scn FROM v$database")
        end = int(cur.fetchone()[0])
    return end, min(before, oldest["scn"]) if oldest else before, oldest


def position(conn) -> tuple[int, int]:
    """``(current SCN, SCN to mine from)``: the second is where the oldest transaction still open started (or the
    SCN before it was looked for), so one that commits later is read whole."""
    end, restart, _ = scan_position(conn)
    return end, restart


def copy_position(conn, desc: Desc, wait: float = 30.0) -> tuple[int, int]:
    """:func:`position`, at an SCN the table can be read as of: right after DDL on the table (its supplemental
    logging, a grant) a flashback query to an SCN read since can still be refused (ORA-01466, the SCN-to-time
    mapping is coarse), so the SCN is read again until it can."""
    end = time.monotonic() + wait
    while True:
        scn, restart = position(conn)
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT 1 FROM {desc.qualified} AS OF SCN :s WHERE ROWNUM < 1", s=scn)
                cur.fetchall()
                return scn, restart
            except Exception as e:  # noqa: BLE001 - only the definition-changed refusal is waited out
                if _code(e) != "ORA-01466" or time.monotonic() > end:
                    raise
        time.sleep(1)


def _expr(col: Col) -> str:
    q = f'"{col.name}"'
    if col.kind in ("str", "nstr", "lob", "blob"):
        return q
    if col.kind == "raw":
        return f"RAWTOHEX({q})"
    return f"TO_CHAR({q})"                                # the NLS text forms typed() reads


def copy(conn, desc: Desc, scn: int, batch: int = 5000):
    """The table as it was at ``scn``, in batches of rows."""
    with conn.cursor() as cur:
        cur.arraysize = batch
        cur.execute(f"SELECT {', '.join(_expr(c) for c in desc.columns)} FROM {desc.qualified} AS OF SCN :scn",
                    scn=scn)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield [{c.out: typed(c, v) for c, v in zip(desc.columns, row)} for row in rows]


def _key_sql(desc: Desc, keyvals: dict) -> tuple[str, dict]:
    conds, binds = [], {}
    for i, k in enumerate(desc.key):
        col = desc.by_out[k]
        b = f"k{i}"
        if col.kind == "raw":
            conds.append(f'"{col.name}" = HEXTORAW(:{b})')
        elif col.kind in ("tstz", "tsltz"):
            conds.append(f'SYS_EXTRACT_UTC("{col.name}") = :{b}')
            keyvals = {**keyvals, k: keyvals[k].replace(tzinfo=None) if keyvals[k] else None}
        else:
            conds.append(f'"{col.name}" = :{b}')
        binds[b] = keyvals[k]
    return " AND ".join(conds), binds


def reread(conn, desc: Desc, keyvals: dict, scn: int, cols: list[Col]) -> Optional[dict]:
    """Columns of one row as of ``scn`` (the current row if the undo for it is gone); None if there is none."""
    where, binds = _key_sql(desc, keyvals)
    select = ", ".join(_expr(c) for c in cols)
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT {select} FROM {desc.qualified} AS OF SCN :scn WHERE {where}", scn=scn, **binds)
        except Exception as e:  # noqa: BLE001 - only a flashback that cannot reach back falls through
            if _code(e) not in FLASHBACK_GONE:
                raise
            cur.execute(f"SELECT {select} FROM {desc.qualified} WHERE {where}", binds)
        row = cur.fetchone()
    return None if row is None else {c.out: typed(c, v) for c, v in zip(cols, row)}


def reread_rowid(conn, desc: Desc, rowid: str, scn: int) -> Optional[dict]:
    """A whole row by its ROWID as of ``scn`` (a LOB written through DBMS_LOB names only the row it changed)."""
    select = ", ".join(_expr(c) for c in desc.columns)
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT {select} FROM {desc.qualified} AS OF SCN :scn WHERE ROWID = CHARTOROWID(:r)",
                        scn=scn, r=rowid)
        except Exception as e:  # noqa: BLE001 - only a flashback that cannot reach back falls through
            if _code(e) not in FLASHBACK_GONE:
                raise
            cur.execute(f"SELECT {select} FROM {desc.qualified} WHERE ROWID = CHARTOROWID(:r)", r=rowid)
        row = cur.fetchone()
    return None if row is None else {c.out: typed(c, v) for c, v in zip(desc.columns, row)}


def contiguous(logs: list[tuple[int, int, int, int]], start: int) -> bool:
    """Whether log files ``(thread, sequence, first SCN, next SCN)`` cover ``start`` onward with no gap: in each
    thread the sequences follow one another and each file starts where the one before it ended."""
    threads: dict = {}
    for t, seq, first, nxt in sorted(logs):
        threads.setdefault(t, []).append((seq, int(first), int(nxt)))
    if not threads:
        return False
    for files in threads.values():
        if files[0][1] > start:
            return False
        for (s1, _, n1), (s2, f2, _) in zip(files, files[1:]):
            if s2 != s1 + 1 or f2 != n1:
                return False
    return True


# ---- LogMiner -----------------------------------------------------------------------------------
def _logs_cover(cur, restart: int) -> bool:
    cur.execute("SELECT MIN(first_change#) FROM (SELECT first_change# FROM v$log WHERE status <> 'UNUSED' "
                "UNION ALL SELECT first_change# FROM v$archived_log WHERE name IS NOT NULL AND deleted = 'NO' "
                "AND status = 'A')")
    low = cur.fetchone()[0]
    return low is not None and int(low) <= restart


def _add_logs(cur, start: int, end: int) -> None:
    """For a non-CDB or the root: the archived logs, then the online ones not archived yet, that cover the range."""
    cur.execute("SELECT thread#, sequence#, MIN(name), MIN(first_change#), MIN(next_change#) FROM v$archived_log "
                "WHERE name IS NOT NULL AND deleted = 'NO' AND status = 'A' AND standby_dest = 'NO' "
                "AND next_change# > :s AND first_change# <= :e GROUP BY thread#, sequence#", s=start, e=end)
    files = {(t, s): (name, first, nxt) for t, s, name, first, nxt in cur.fetchall()}
    cur.execute("SELECT l.thread#, l.sequence#, MIN(f.member), MIN(l.first_change#), MIN(l.next_change#) "
                "FROM v$log l JOIN v$logfile f ON f.group# = l.group# "
                "WHERE l.next_change# > :s AND l.first_change# <= :e AND l.status <> 'UNUSED' "
                "GROUP BY l.thread#, l.sequence#", s=start, e=end)
    for t, s, name, first, nxt in cur.fetchall():
        files.setdefault((t, s), (name, first, nxt))
    if not contiguous([(t, s, f, n) for (t, s), (_, f, n) in files.items()], start):
        raise RuntimeError(f"{GONE} (the logs from SCN {start} have a gap)")
    files = {k: v[0] for k, v in files.items()}
    for i, (_, name) in enumerate(sorted(files.items())):
        cur.execute("BEGIN DBMS_LOGMNR.ADD_LOGFILE(LOGFILENAME => :f, OPTIONS => :o); END;", f=name,
                    o=1 if i == 0 else 3)                 # NEW, then ADDFILE


def changes(conn, desc: Desc, applied: int, restart: int) -> tuple[list, Optional[tuple], Optional[str]]:
    """Whole transactions committed after ``applied``, mined from ``restart``: ``([(commit SCN, op, row)],
    (applied, restart) to record or None, why the mirror is holding or None)``; ``op`` is upsert, delete or
    truncate.

    Where it may record it has read to is proved, not timed: a commit whose SCN is at most ``end`` can still be on
    its way to the redo log (``COMMIT WRITE NOWAIT``, a slow log disk) when LogMiner reads. LogMiner returns the
    COMMIT record of every transaction it read; the redo log is written in order, so once a commit at SCN ``m`` is
    in what LogMiner read, every commit before ``m`` is too. The mirror records ``end`` only when LogMiner read a
    commit at or after it; otherwise it records the highest commit SCN LogMiner read and keeps its restart point,
    so the next run reads the rest."""
    end, next_restart, oldest = scan_position(conn)
    holding = None
    if oldest and oldest["age"] > HOLD_AFTER:
        holding = (f"holding: the open transaction {oldest['xid']}"
                   + (f" (session {oldest['session']})" if oldest["session"] else "")
                   + f" started {oldest['age'] // 60} minutes ago at SCN {oldest['scn']}; the mirror reads the redo "
                   "from its start on every run so that its changes arrive when it commits. Commit or roll it back "
                   "(or end its session) to release it")
    if end <= applied:
        return [], None, holding
    cols = [c for c in desc.columns if c.kind not in LOBS]
    names = {f"c{i}": f'"{desc.owner}"."{desc.table}"."{c.name}"' for i, c in enumerate(cols)}
    values = ", ".join(f"DBMS_LOGMNR.MINE_VALUE(redo_value, :c{i}), DBMS_LOGMNR.COLUMN_PRESENT(redo_value, :c{i}), "
                       f"DBMS_LOGMNR.MINE_VALUE(undo_value, :c{i}), DBMS_LOGMNR.COLUMN_PRESENT(undo_value, :c{i})"
                       for i in range(len(cols)))
    got: list = []
    stopped: Optional[int] = None
    with conn.cursor() as cur:
        if not _logs_cover(cur, restart):
            raise RuntimeError(GONE)
        try:
            if not desc.pdb:
                _add_logs(cur, restart, end)
            cur.execute("BEGIN DBMS_LOGMNR.START_LOGMNR(STARTSCN => :s, ENDSCN => :e, OPTIONS => "
                        "DBMS_LOGMNR.DICT_FROM_ONLINE_CATALOG + DBMS_LOGMNR.COMMITTED_DATA_ONLY); END;",
                        s=restart, e=end)
        except Exception as e:  # noqa: BLE001 - a log that cannot be found is the one message that helps
            if _gone(e):
                raise RuntimeError(f"{GONE} ({_gone(e)})")
            raise
        try:
            cur.arraysize = 1000
            ops = ", ".join(f"'{o}'" for o in ("INSERT", "UPDATE", "DELETE", "DDL", "UNSUPPORTED") + LOB_OPS)
            cur.execute(f"SELECT commit_scn, operation, csf, info, sql_redo, row_id, scn"
                        f"{', ' + values if values else ''} FROM v$logmnr_contents "
                        f"WHERE (seg_owner = :o AND table_name = :t AND commit_scn > :a AND operation IN ({ops})) "
                        "OR operation IN ('COMMIT', 'MISSING_SCN')",
                        o=desc.owner, t=desc.table, a=applied, **names)
            chain: Optional[list] = None
            current: Optional[int] = None
            whole = 0                                     # rows of transactions read to their end
            stop_after = False
            seen = applied                                # the highest commit LogMiner read
            for row in cur:
                if row[1] == "MISSING_SCN":
                    raise RuntimeError(f"{GONE} (LogMiner found a gap in the redo: {row[3] or 'missing SCNs'})")
                if row[1] == "COMMIT":
                    seen = max(seen, int(row[6] or 0))
                    continue
                if chain is not None:                     # a statement LogMiner continued over several rows
                    chain[4] = (chain[4] or "") + (row[4] or "")
                    if row[2]:
                        continue
                    row, chain = tuple(chain), None
                elif row[2]:
                    chain = list(row)
                    continue
                scn = int(row[0])
                if scn != current:
                    if current is not None:
                        whole = len(got)
                        if stop_after or whole >= MAX_ROWS:
                            stopped = current
                            break
                    current = scn
                got.append(row)
                if row[1] == "DDL" and ddl_effect(row[4] or "") == "add":
                    stop_after = True                     # the next run reads the table's new columns first
            else:
                whole = len(got)
            del got[whole:]
        except Exception as e:  # noqa: BLE001
            if _gone(e):
                raise RuntimeError(f"{GONE} ({_gone(e)})")
            raise
        finally:
            try:
                cur.execute("BEGIN DBMS_LOGMNR.END_LOGMNR; END;")
            except Exception:  # noqa: BLE001 - the session ends with the connection anyway
                pass
    out = _apply(conn, desc, cols, got)
    if stopped is not None:
        return out, (stopped, restart), holding
    if seen >= end:
        return out, (end, next_restart), holding
    # LogMiner has not yet read a commit at or after end: what is proved is up to the last commit it read.
    last = max([applied, seen] + [o[0] for o in out])
    return out, ((last, restart) if last > applied else None), holding


def _apply(conn, desc: Desc, cols: list[Col], got: list) -> list:
    """LogMiner rows as ``(commit SCN, op, row)``, LOBs and columns an update did not carry read back by key."""
    out: list = []
    lob_rows: set = set()
    lobs = [c for c in desc.columns if c.kind in LOBS]
    where = f"{desc.owner}.{desc.table}"
    for row in got:
        scn, op, _, info, redo_sql, rowid = int(row[0]), row[1], row[2], row[3] or "", row[4] or "", row[5]
        if op in LOB_OPS:
            # A LOB written through DBMS_LOB (a locator selected FOR UPDATE, then WRITE, WRITEAPPEND, TRIM or
            # ERASE): the row is read back whole, as of the commit.
            if rowid and rowid.strip("A") and (scn, rowid) not in lob_rows:
                lob_rows.add((scn, rowid))
                back = reread_rowid(conn, desc, rowid, scn)
                if back is not None:
                    out.append((scn, "upsert", back))
            continue
        if op == "UNSUPPORTED":
            raise RuntimeError(f"LogMiner could not read a change to {where} ({info or 'unsupported'}) committed at "
                               f"SCN {scn}: delete the mirror and create it again")
        if op == "DDL":
            effect = ddl_effect(redo_sql)
            if effect == "truncate":
                out.append((scn, "truncate", {}))
            elif effect == "fail":
                raise RuntimeError(f"a DDL statement changed {where} in a way a mirror cannot follow "
                                   f"({redo_sql.strip()[:200]}): delete the mirror and create it again")
            continue
        new, old = {}, {}
        vals = row[7:]
        for i, c in enumerate(cols):
            rv, rp, uv, up = vals[4 * i:4 * i + 4]
            if rp == 1:
                new[c.out] = typed(c, rv)
            if up == 1:
                old[c.out] = typed(c, uv)
        if not new and not old or "Dictionary" in info:
            # LogMiner could not map the change with today's dictionary: its statement carries the values.
            _, _, parsed_new, parsed_old = parse_dml(redo_sql)
            new, old = statement_values(desc, parsed_new), statement_values(desc, parsed_old)
        if op == "DELETE":
            before = {**new, **old}
            if any(before.get(k) is None for k in desc.key):
                raise RuntimeError(f"a delete from {where} carried no primary key: supplemental logging is off")
            out.append((scn, "delete", {k: before[k] for k in desc.key}))
            continue
        if op == "UPDATE":
            after = {**old, **new}
            key_old = {k: old.get(k, new.get(k)) for k in desc.key}
            if any(v is None for v in key_old.values()):
                raise RuntimeError(f"an update to {where} carried no primary key: supplemental logging is off")
            if any(k in new and k in old and new[k] != old[k] for k in desc.key):   # the key changed
                out.append((scn, "delete", key_old))
        else:
            after = new
        missing = [c for c in desc.columns if c.out not in after and c.kind not in LOBS] + lobs
        if missing and all(after.get(k) is not None for k in desc.key):
            back = reread(conn, desc, {k: after[k] for k in desc.key}, scn, missing)
            if back is not None:
                after = {**after, **back}
        out.append((scn, "upsert", after))
    return out
