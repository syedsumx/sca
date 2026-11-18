"""Db2 as a mirror source: a Db2 for Linux, UNIX and Windows table kept in a lakehouse table through SQL Replication's
Capture program, which reads the database's recovery log.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``db2://``:
``db2://user:password@host:50000/DATABASE`` (``?capture_schema=ASN`` when Capture's control tables are in another
schema). Capture ships with Db2. A database administrator, once per database and table:

* turns on archive logging (``LOGARCHMETH1``) so Capture can read what it has not read yet;
* creates Capture's control tables and registers the table, with the change table holding every column and the
  before image of the key, then starts Capture::

      asnclp: SET SERVER CAPTURE TO DB <db>; SET RUN SCRIPT NOW STOP ON SQL ERROR ON;
              CREATE CONTROL TABLES FOR CAPTURE SERVER;
              CREATE REGISTRATION (<SCHEMA>.<TABLE>) DIFFERENTIAL REFRESH STAGE CD<TABLE> COLS ALL IMAGE BOTH;
      asncap capture_server=<db> capture_schema=ASN

Cairn then acts as the registration's subscriber, as SQL Replication's Apply would: it adds its own row to
``IBMSNAP_PRUNCNTL`` and sends Capture a ``CAPSTART`` signal, so Capture starts capturing the table (if it was not
already) and keeps every change until Cairn has applied it. Cairn moves its row's synchpoint forward after each
commit, so Capture prunes what the mirror has read and nothing it has not, and removes the row when the mirror is
deleted. The connection's user needs ``SELECT`` on the table, its change table and the control tables, ``INSERT`` on
``IBMSNAP_SIGNAL``, and ``INSERT``, ``UPDATE`` and ``DELETE`` on ``IBMSNAP_PRUNCNTL``.

* **The copy.** Taken once Capture has processed the signal, after a ``SHARE`` lock taken and released at once on
  the table: every transaction that wrote the table before Capture started capturing it has then committed, so the
  copy holds it, and every change after is in the change table. Changes replayed over the copy converge on it.
* **Changes.** Rows of the change table with a commit sequence after the mirror's, up to the highest commit Capture
  has recorded (``IBMSNAP_RESTART``), in commit order, whole transactions at a time. Offsets are commit log
  positions. An update that changed the key deletes the old key (from the before image, or Capture's delete and
  insert when the registration has ``UPDATE AS DELETE INSERT ON``).
* **When the changes are gone.** Capture's retention limit (a week by default) prunes changes even if a mirror has
  not read them, and a cold start of Capture deactivates registrations. Either way the change table no longer holds
  everything after the mirror's position: the mirror starts the table again (a new signal and a new copy), as Apply
  would with a full refresh, and the audit log says so.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

MAX_ROWS = 20_000
URL = re.compile(r"^(db2|ibm_db_sa|db2\+\w+)://")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]{0,127}$")
ZERO = b"\x00" * 16
# The statements a database administrator runs, said in a refusal.
REGISTER = ("register it for SQL Replication with every column and the key's before image, and start Capture: "
            "asnclp \"SET SERVER CAPTURE TO DB <database>; SET RUN SCRIPT NOW STOP ON SQL ERROR ON; "
            "CREATE REGISTRATION ({schema}.{table}) DIFFERENTIAL REFRESH STAGE CD{table} COLS ALL IMAGE BOTH;\" "
            "(a table with LOB or XML columns: COLS (<key column> IMAGE BOTH, <LOB column> IMAGE AFTER, ...)), "
            "then asncap capture_server=<database> capture_schema={capschema}")
CONTROL = ("Capture's control tables are not in schema {capschema}: create them (asnclp \"SET SERVER CAPTURE TO DB "
           "<database>; SET RUN SCRIPT NOW STOP ON SQL ERROR ON; CREATE CONTROL TABLES FOR CAPTURE SERVER;\"), "
           "or name their schema in the connection (?capture_schema=...)")


def q(name: str) -> str:
    """A Db2 identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


class Restart(Exception):
    """The change table no longer holds every change after the mirror's position: the table is started again."""


@dataclass
class Col:
    name: str               # as Db2 has it (upper case, usually)
    out: str                # the lakehouse column: lower case
    kind: str               # int dec float bool date ts time text bin
    precision: Optional[int] = None
    scale: int = 0
    in_cd: bool = True      # in the change table (LOB and XML columns are not: read back from the table by key)


@dataclass
class Desc:
    schema: str
    table: str
    capschema: str
    columns: list[Col]
    key: list[str]                  # Db2 names
    cd_owner: str
    cd_table: str
    before: dict                    # Db2 key column -> its before-image column in the change table
    del_ins: bool                   # Capture writes an update as a delete and an insert
    state: str                      # the registration's: A active, I inactive, S stopped
    by_name: dict = field(default_factory=dict)

    def __post_init__(self):
        self.by_name = {c.name: c for c in self.columns}

    @property
    def qualified(self) -> str:
        return f"{q(self.schema)}.{q(self.table)}"

    @property
    def cd(self) -> str:
        return f"{q(self.cd_owner)}.{q(self.cd_table)}"

    @property
    def out_key(self) -> list[str]:
        return [self.by_name[k].out for k in self.key]


# ---- connections ------------------------------------------------------------------------------
def settings(url: str) -> dict:
    u = urlsplit(URL.sub("db2://", url))
    if u.scheme != "db2":
        raise ValueError("not a db2:// URL")
    database = (u.path or "/").lstrip("/")
    if not database:
        raise ValueError("a db2:// URL names its database: db2://user:password@host:50000/DATABASE")
    capschema = (parse_qs(u.query).get("capture_schema") or ["ASN"])[0]
    if not IDENT.match(capschema):
        raise ValueError("capture_schema is a schema name")
    return {"host": u.hostname or "localhost", "port": u.port or 50000, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": database, "capschema": capschema.upper()}


def connect(url: str):
    """A connection, its user and password given apart from the connection string (which cannot quote them)."""
    try:
        import ibm_db_dbi
    except ImportError:
        raise ValueError("Db2 mirrors need IBM's ibm_db driver, which IBM publishes for x86-64 Linux, macOS and "
                         "Windows: run Cairn's API on one of those") from None
    s = settings(url)
    for part in (s["host"], s["database"]):
        if not re.match(r"^[A-Za-z0-9_.\-:\[\]]+$", part):
            raise ValueError("a db2:// URL's host and database are plain names")
    dsn = (f"DATABASE={s['database']};HOSTNAME={s['host']};PORT={int(s['port'])};PROTOCOL=TCPIP;"
           "CONNECTTIMEOUT=10;QUERYTIMEOUT=600;")
    conn = ibm_db_dbi.connect(dsn, s["user"], s["password"])
    conn.set_autocommit(True)
    return conn


def offset(seq: bytes) -> int:
    """A commit sequence as a stream offset: its log position (the lower 8 bytes, which rise with every commit on a
    single-member database; the upper 8 count log flushes, which several commits can share)."""
    return min(int.from_bytes(bytes(seq)[-8:], "big"), 2 ** 63 - 1)


def seq_hex(seq: bytes) -> str:
    return "0x" + bytes(seq).hex().upper()


def seq_bytes(text: str) -> bytes:
    return bytes.fromhex(text[2:] if text.lower().startswith("0x") else text)


# ---- what a table needs -----------------------------------------------------------------------
def kind(typename: str, codepage: int) -> tuple[str, Optional[int]]:
    """How a Db2 column lands: ``(kind, precision)`` (``None``: read as the driver gives it, as text)."""
    t = typename.upper().strip()
    if t in ("SMALLINT", "INTEGER", "BIGINT"):
        return "int", None
    if t in ("DECIMAL", "NUMERIC"):
        return "dec", None
    if t in ("REAL", "DOUBLE", "FLOAT"):
        return "float", None
    if t == "BOOLEAN":
        return "bool", None
    if t == "DATE":
        return "date", None
    if t == "TIMESTAMP":
        return "ts", None
    if t == "TIME":
        return "time", None
    if t in ("BINARY", "VARBINARY", "BLOB") or (t in ("CHARACTER", "VARCHAR", "CHAR") and codepage == 0):
        return "bin", None
    return "text", None                     # CHAR, VARCHAR, GRAPHIC, CLOB, DBCLOB, XML, DECFLOAT (its exact text)


LOBS = ("BLOB", "CLOB", "DBCLOB", "XML")


def arrow_schema(desc: Desc):
    """The lakehouse table's columns, typed from Db2's (a column that is NULL in every row copied keeps its type)."""
    import pyarrow as pa

    types = {"int": pa.int64(), "float": pa.float64(), "bool": pa.bool_(), "date": pa.date32(),
             "ts": pa.timestamp("us")}
    return pa.schema([pa.field(c.out, pa.decimal128(c.precision or 31, c.scale) if c.kind == "dec"
                               else types.get(c.kind, pa.string())) for c in desc.columns])


def value(col: Col, v: Any) -> Any:
    """A value as the driver read it, as the lakehouse column holds it."""
    if v is None:
        return None
    if col.kind == "dec":
        return v if isinstance(v, Decimal) else Decimal(str(v))
    if col.kind == "float":
        return float(v)
    if col.kind == "bin":
        return bytes(v).hex()
    if col.kind == "time":
        return v.isoformat() if isinstance(v, dt.time) else str(v)
    if col.kind == "text" and isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if col.kind == "text" and not isinstance(v, str):
        return str(v)
    return v


def _one(cur, sql: str, params: tuple = ()) -> Optional[tuple]:
    cur.execute(sql, params)
    return cur.fetchone()


def check(conn, schema: str, table: str, capschema: str) -> Desc:
    """The table, its registration and its change table, after checking the mirror can work from them; a
    ``ValueError`` says what to run otherwise."""
    cur = conn.cursor()
    try:
        got = _one(cur, "SELECT TABSCHEMA, TABNAME FROM SYSCAT.TABLES WHERE TYPE = 'T' AND "
                        "((TABSCHEMA = ? AND TABNAME = ?) OR (TABSCHEMA = ? AND TABNAME = ?))",
                   (schema, table, schema.upper(), table.upper()))
        if not got:
            raise ValueError(f"{schema}.{table} is not a table the connection's user can see")
        schema, table = got[0].strip(), got[1].strip()
        if not _one(cur, "SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = 'IBMSNAP_REGISTER'",
                    (capschema,)):
            raise ValueError(CONTROL.format(capschema=capschema))
        reg = _one(cur, f'SELECT CD_OWNER, CD_TABLE, BEFORE_IMG_PREFIX, CHG_UPD_TO_DEL_INS, STATE, SOURCE_STRUCTURE '
                        f'FROM "{capschema}".IBMSNAP_REGISTER WHERE SOURCE_OWNER = ? AND SOURCE_TABLE = ? '
                        f"AND SOURCE_VIEW_QUAL = 0 AND GLOBAL_RECORD = 'N'", (schema, table))
        if not reg or not reg[0] or not reg[1]:
            raise ValueError(f"{schema}.{table} is not registered for SQL Replication: "
                             + REGISTER.format(schema=schema, table=table, capschema=capschema))
        cd_owner, cd_table = reg[0].strip(), reg[1].strip()
        prefix, del_ins, state = (reg[2] or "").strip(), (reg[3] or "N").strip() == "Y", (reg[4] or "I").strip()
        if int(reg[5] or 1) != 1:
            raise ValueError(f"{schema}.{table}'s registration is not of a user table (source structure {reg[5]})")
        dc = _one(cur, "SELECT DATACAPTURE FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = ?", (schema, table))
        if not dc or dc[0].strip() not in ("Y", "L"):
            raise ValueError(f"{schema}.{table} is not logged with its changes: "
                             f"ALTER TABLE {q(schema)}.{q(table)} DATA CAPTURE CHANGES")
        cur.execute("SELECT COLNAME, TYPENAME, LENGTH, SCALE, CODEPAGE, KEYSEQ FROM SYSCAT.COLUMNS "
                    "WHERE TABSCHEMA = ? AND TABNAME = ? ORDER BY COLNO", (schema, table))
        cols_raw = cur.fetchall()
        cur.execute("SELECT COLNAME FROM SYSCAT.COLUMNS WHERE TABSCHEMA = ? AND TABNAME = ?", (cd_owner, cd_table))
        cd_cols = {r[0].strip() for r in cur.fetchall()}
        if not cd_cols:
            raise ValueError(f"the change table {cd_owner}.{cd_table} is missing: register {schema}.{table} again")
    finally:
        cur.close()
    columns, key = [], []
    names = {r[0].strip() for r in cols_raw}
    for name, typename, length, scale, codepage, keyseq in cols_raw:
        name, typename = name.strip(), typename.strip()
        k, _ = kind(typename, int(codepage or 0))
        # Capture records only that a LOB or XML value changed (a one-character column): it is read back by key.
        col = Col(name=name, out=name.lower(), kind=k, in_cd=name in cd_cols and typename.upper() not in LOBS)
        if k == "dec":
            col.precision, col.scale = min(int(length), 38), int(scale or 0)
        if not col.in_cd and typename.upper() not in LOBS:
            raise ValueError(f"{schema}.{table}'s change table has no column {name}: register the table again "
                             "with every column (COLS ALL)")
        if keyseq:
            key.append((int(keyseq), name))
        columns.append(col)
    if len({c.out for c in columns}) != len(columns):
        raise ValueError(f"{schema}.{table} has columns whose names differ only in case")
    key_names = [n for _, n in sorted(key)]
    if not key_names:
        raise ValueError(f"{schema}.{table} has no primary key: a mirror applies changes by key")
    before = {k: prefix + k for k in key_names if prefix and prefix + k in cd_cols and prefix + k not in names}
    if len(before) < len(key_names) and not del_ins:
        raise ValueError(f"{schema}.{table}'s registration records no before image of its key, so an update that "
                         "changes the key could not be followed: register it again with IMAGE BOTH (with LOB or XML "
                         "columns, IMAGE BOTH for the key columns and IMAGE AFTER for the others)")
    return Desc(schema=schema, table=table, capschema=capschema, columns=columns, key=key_names, cd_owner=cd_owner,
                cd_table=cd_table, before=before, del_ins=del_ins, state=state)


def privileges(conn, desc: Desc) -> list[str]:
    """What the connection's user may not do that a mirror needs, tried in a transaction that is rolled back."""
    missing = []
    cur = conn.cursor()
    conn.set_autocommit(False)
    cap = f'"{desc.capschema}"'
    tries = [(f"SELECT 1 FROM {desc.qualified} FETCH FIRST 1 ROW ONLY", (), f"SELECT ON {desc.schema}.{desc.table}"),
             (f"SELECT 1 FROM {desc.cd} FETCH FIRST 1 ROW ONLY", (), f"SELECT ON {desc.cd_owner}.{desc.cd_table}"),
             (f"SELECT MAX_COMMITSEQ FROM {cap}.IBMSNAP_RESTART", (), f"SELECT ON {desc.capschema}.IBMSNAP_RESTART"),
             (f"INSERT INTO {cap}.IBMSNAP_SIGNAL (SIGNAL_TIME, SIGNAL_TYPE, SIGNAL_SUBTYPE, SIGNAL_INPUT_IN, "
              "SIGNAL_STATE) VALUES (CURRENT TIMESTAMP, 'USER', 'CAIRN', 'privilege check', 'C')", (),
              f"INSERT ON {desc.capschema}.IBMSNAP_SIGNAL"),
             (f"INSERT INTO {cap}.IBMSNAP_PRUNCNTL (TARGET_SERVER, TARGET_OWNER, TARGET_TABLE, SOURCE_OWNER, "
              "SOURCE_TABLE, SOURCE_VIEW_QUAL, APPLY_QUAL, SET_NAME, CNTL_SERVER, TARGET_STRUCTURE, PHYS_CHANGE_OWNER, "
              "PHYS_CHANGE_TABLE, MAP_ID) VALUES ('CAIRN', 'CAIRN', 'privilege-check', ?, ?, 0, 'CAIRN', 'CHECK', "
              "'CAIRN', 8, ?, ?, 'CAIRNCHECK')", (desc.schema, desc.table, desc.cd_owner, desc.cd_table),
              f"INSERT ON {desc.capschema}.IBMSNAP_PRUNCNTL"),
             (f"UPDATE {cap}.IBMSNAP_PRUNCNTL SET SYNCHTIME = CURRENT TIMESTAMP WHERE MAP_ID = 'CAIRNCHECK'", (),
              f"UPDATE ON {desc.capschema}.IBMSNAP_PRUNCNTL"),
             (f"DELETE FROM {cap}.IBMSNAP_PRUNCNTL WHERE MAP_ID = 'CAIRNCHECK'", (),
              f"DELETE ON {desc.capschema}.IBMSNAP_PRUNCNTL")]
    try:
        for sql, params, grant in tries:
            cur.execute("SAVEPOINT cairn_check ON ROLLBACK RETAIN CURSORS")
            try:
                cur.execute(sql, params)
                if sql.startswith("SELECT"):
                    cur.fetchall()
            except Exception as e:  # noqa: BLE001 - an authorization error says what is missing; others are raised
                if not re.search(r"SQL0551N|SQL0552N|SQLSTATE=42501", str(e)):
                    raise
                missing.append(grant)
            cur.execute("ROLLBACK TO SAVEPOINT cairn_check")
    finally:
        conn.rollback()
        conn.set_autocommit(True)
        cur.close()
    return missing


# ---- the subscription ---------------------------------------------------------------------------
def map_id(mid: str) -> str:
    return ("C" + mid)[:10].upper()


def subscribe(conn, desc: Desc, mid: str, name: str, database: str) -> Optional[bytes]:
    """Cairn's row in ``IBMSNAP_PRUNCNTL`` and a ``CAPSTART`` signal for it, once. Returns the log position from which
    Capture captures for the mirror (the row's synchpoint, set when it processed the signal), or ``None`` while
    Capture has not processed it yet."""
    cap = f'"{desc.capschema}"'
    cur = conn.cursor()
    try:
        got = _one(cur, f"SELECT SYNCHPOINT FROM {cap}.IBMSNAP_PRUNCNTL WHERE MAP_ID = ?", (map_id(mid),))
        if got is not None:
            return bytes(got[0]) if got[0] is not None else None
        conn.set_autocommit(False)
        try:
            cur.execute(f"INSERT INTO {cap}.IBMSNAP_PRUNCNTL (TARGET_SERVER, TARGET_OWNER, TARGET_TABLE, SOURCE_OWNER, "
                        "SOURCE_TABLE, SOURCE_VIEW_QUAL, APPLY_QUAL, SET_NAME, CNTL_SERVER, TARGET_STRUCTURE, "
                        "PHYS_CHANGE_OWNER, PHYS_CHANGE_TABLE, MAP_ID) VALUES ('CAIRN', 'CAIRN', ?, ?, ?, 0, 'CAIRN', "
                        "?, ?, 8, ?, ?, ?)", (name, desc.schema, desc.table, mid[:18].upper(), database[:18].upper(),
                                              desc.cd_owner, desc.cd_table, map_id(mid)))
            cur.execute(f"INSERT INTO {cap}.IBMSNAP_SIGNAL (SIGNAL_TIME, SIGNAL_TYPE, SIGNAL_SUBTYPE, SIGNAL_INPUT_IN, "
                        "SIGNAL_STATE) VALUES (CURRENT TIMESTAMP, 'CMD', 'CAPSTART', ?, 'P')", (map_id(mid),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.set_autocommit(True)
        return None
    finally:
        cur.close()


def unsubscribe(conn, capschema: str, mid: str) -> None:
    """Cairn's row goes, so Capture no longer keeps changes for this mirror."""
    cur = conn.cursor()
    try:
        cur.execute(f'DELETE FROM "{capschema}".IBMSNAP_PRUNCNTL WHERE MAP_ID = ?', (map_id(mid),))
    finally:
        cur.close()


def advance(conn, capschema: str, mid: str, seq: bytes) -> None:
    """Changes up to ``seq`` are in the lakehouse: Capture may prune them."""
    cur = conn.cursor()
    try:
        cur.execute(f'UPDATE "{capschema}".IBMSNAP_PRUNCNTL SET SYNCHPOINT = ?, SYNCHTIME = CURRENT TIMESTAMP '
                    "WHERE MAP_ID = ?", (bytes(seq), map_id(mid)))
    finally:
        cur.close()


# ---- the copy and the changes -------------------------------------------------------------------
def barrier(conn, desc: Desc, wait_seconds: int = 60) -> None:
    """Waits for every transaction that wrote the table to end (a ``SHARE`` lock, released at once): those that began
    before Capture captured the table are then committed or rolled back, and a copy taken after holds them."""
    cur = conn.cursor()
    conn.set_autocommit(False)
    try:
        cur.execute(f"SET CURRENT LOCK TIMEOUT {int(wait_seconds)}")
        cur.execute(f"LOCK TABLE {desc.qualified} IN SHARE MODE")
    finally:
        conn.rollback()
        cur.execute("SET CURRENT LOCK TIMEOUT NULL")
        conn.set_autocommit(True)
        cur.close()


def _select(desc: Desc) -> str:
    return ", ".join(q(c.name) for c in desc.columns)


def copy(conn, desc: Desc, batch: int = 5000):
    """The table's rows, in batches of lakehouse rows."""
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {_select(desc)} FROM {desc.qualified}")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield [{c.out: value(c, v) for c, v in zip(desc.columns, r)} for r in rows]
    finally:
        cur.close()


def registration(conn, desc: Desc) -> tuple[str, Optional[bytes]]:
    """The registration's state and the oldest change its change table holds (``CD_OLD_SYNCHPOINT``)."""
    cur = conn.cursor()
    try:
        got = _one(cur, f'SELECT STATE, CD_OLD_SYNCHPOINT FROM "{desc.capschema}".IBMSNAP_REGISTER '
                        "WHERE SOURCE_OWNER = ? AND SOURCE_TABLE = ? AND SOURCE_VIEW_QUAL = 0 AND GLOBAL_RECORD = 'N'",
                   (desc.schema, desc.table))
    finally:
        cur.close()
    if not got:
        return "gone", None
    return (got[0] or "I").strip(), bytes(got[1]) if got[1] is not None else None


def reread(conn, desc: Desc, row: dict) -> dict:
    """The columns the change table does not hold (LOBs, XML), read back from the table by key: their latest value,
    on which the mirror converges."""
    lobs = [c for c in desc.columns if not c.in_cd]
    if not lobs:
        return row
    cur = conn.cursor()
    try:
        got = _one(cur, f"SELECT {', '.join(q(c.name) for c in lobs)} FROM {desc.qualified} WHERE "
                        + " AND ".join(f"{q(k)} = ?" for k in desc.key),
                   tuple(row[desc.by_name[k].out] if desc.by_name[k].kind != "bin" else bytes.fromhex(row[desc.by_name[k].out])
                         for k in desc.key))
    finally:
        cur.close()
    if got is None:
        return row                                         # deleted since: its delete follows
    return {**row, **{c.out: value(c, v) for c, v in zip(lobs, got)}}


def changes(conn, desc: Desc, after: bytes, max_rows: int = MAX_ROWS) -> tuple[list, bytes]:
    """Whole transactions committed after ``after``, in commit order: ``([(offset, op, row)], position)``, where the
    position is the last commit read (or the highest Capture has recorded, when everything up to it was read).
    Raises :class:`Restart` when the change table no longer holds everything after ``after``."""
    state, oldest = registration(conn, desc)
    if state != "A":
        raise Restart(f"the registration of {desc.schema}.{desc.table} is not active (state {state}): Capture "
                      "stopped capturing it")
    if oldest is not None and bytes(after) != ZERO and oldest > bytes(after):
        raise Restart(f"the change table of {desc.schema}.{desc.table} holds changes from {seq_hex(oldest)} only, "
                      f"after the mirror's {seq_hex(after)}")
    cur = conn.cursor()
    try:
        got = _one(cur, f'SELECT MAX_COMMITSEQ FROM "{desc.capschema}".IBMSNAP_RESTART')
        if not got or got[0] is None:
            return [], after
        high = bytes(got[0])
        if high <= bytes(after):
            return [], after
        cd_names = [c.name for c in desc.columns if c.in_cd]
        befores = [desc.before[k] for k in desc.key if k in desc.before]
        cur.execute("SELECT IBMSNAP_COMMITSEQ, IBMSNAP_OPERATION, "
                    + ", ".join(q(n) for n in cd_names + befores)
                    + f" FROM {desc.cd} WHERE IBMSNAP_COMMITSEQ > ? AND IBMSNAP_COMMITSEQ <= ? "
                    "ORDER BY IBMSNAP_COMMITSEQ, IBMSNAP_INTENTSEQ", (bytes(after), high))
        out: list = []
        end = bytes(after)
        current: Optional[bytes] = None
        cut = False
        while not cut:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                seq = bytes(r[0])
                if seq != current:
                    if current is not None:
                        end = current                      # the previous transaction is whole
                        if len(out) >= max_rows:
                            cut = True
                            break
                    current = seq
                at, op = offset(seq), (r[1] or "").strip()
                row = {desc.by_name[n].out: value(desc.by_name[n], v) for n, v in zip(cd_names, r[2:2 + len(cd_names)])}
                if op == "D":
                    out.append((at, "delete", {k: row[k] for k in desc.out_key}))
                    continue
                if op == "U" and befores:
                    old = dict(zip([k for k in desc.key if k in desc.before], r[2 + len(cd_names):]))
                    old = {desc.by_name[k].out: value(desc.by_name[k], v) for k, v in old.items()}
                    if any(old.get(k) is not None and old.get(k) != row.get(k) for k in desc.out_key):
                        out.append((at, "delete", old))       # the key changed: the old row goes
                out.append((at, "upsert", row))
        if not cut:
            end = high                                     # everything Capture has recorded was read
    finally:
        cur.close()
    if any(not c.in_cd for c in desc.columns):
        out = [(at, op, reread(conn, desc, row) if op == "upsert" else row) for at, op, row in out]
    return out, end
