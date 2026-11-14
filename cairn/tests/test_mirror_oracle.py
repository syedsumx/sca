"""Oracle mirrors, the parts that need no database: the tokenizer and parser for the LogMiner statements a mirror
reads when LogMiner could not map a change with today's dictionary, Oracle's internal value formats, the text forms
values arrive in, and how DDL on the mirrored table is taken. The end-to-end test against a real Oracle is
``test_mirror_oracle_e2e.py``."""

import datetime as dt

import pytest

from cairn.pipelines import mirror_oracle as ora

UTC = dt.timezone.utc


def desc():
    cols = [ora.Col("ID", "id", "NUMBER", "int", 1), ora.Col("AMOUNT", "amount", "NUMBER", "float", 2),
            ora.Col("NAME", "name", "VARCHAR2", "str", 3), ora.Col("BORN", "born", "DATE", "date", 4),
            ora.Col("SEEN", "seen", "TIMESTAMP(6)", "ts", 5),
            ora.Col("AT", "at", "TIMESTAMP(6) WITH TIME ZONE", "tstz", 6), ora.Col("TAG", "tag", "RAW", "raw", 7),
            ora.Col("NOTES", "notes", "CLOB", "lob", 8), ora.Col("RATIO", "ratio", "BINARY_DOUBLE", "float", 9),
            ora.Col("nick", "nick", "NVARCHAR2", "nstr", 10)]
    return ora.Desc("SHOP", "ORDERS", cols, ["id"], True)


def test_tokens_quoted_names_strings_numbers():
    toks = ora.tokenize('''insert into "SHOP"."ORD""ERS"("ID","NAME") values ('1','it''s
a "line"', -2.5e3, NULL);''')
    assert toks[:4] == [("word", "INSERT"), ("word", "INTO"), ("id", "SHOP"), ("p", ".")]
    assert ("id", 'ORD"ERS') in toks
    assert ("str", 'it\'s\na "line"') in toks
    assert ("num", "-2.5e3") in toks and ("word", "NULL") in toks
    with pytest.raises(ora.ParseError):
        ora.tokenize("values ('open")


def test_insert_update_delete_shapes():
    op, table, new, old = ora.parse_dml(
        '''insert into "SHOP"."ORDERS"("ID","AMOUNT","NAME","BORN","SEEN","AT","TAG","NOTES") values '''
        """('1','3.5','it''s',TO_DATE('2026-01-02 00:00:00', 'YYYY-MM-DD HH24:MI:SS'),"""
        """TO_TIMESTAMP('2026-01-02 03:04:05.123456'),TO_TIMESTAMP_TZ('2026-01-02 03:04:05.000000 +02:00'),"""
        """HEXTORAW('deadbeef'),EMPTY_CLOB());""")
    assert op == "insert" and table == ["SHOP", "ORDERS"] and old == {}
    row = ora.statement_values(desc(), new)
    assert row == {"id": 1, "amount": 3.5, "name": "it's", "born": dt.datetime(2026, 1, 2),
                   "seen": dt.datetime(2026, 1, 2, 3, 4, 5, 123456),
                   "at": dt.datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC), "tag": "deadbeef"}   # the CLOB is read back
    op, _, new, old = ora.parse_dml('''update "SHOP"."ORDERS" set "AMOUNT" = '7', "NAME" = NULL where "ID" = '1' '''
                                    '''and "AMOUNT" = '3.5' and "TAG" IS NULL and ROWID = 'AAAR6FAAYAAAAAOAAA';''')
    assert op == "update" and ora.statement_values(desc(), new) == {"amount": 7.0, "name": None}
    assert ora.statement_values(desc(), old) == {"id": 1, "amount": 3.5, "tag": None}
    op, _, new, old = ora.parse_dml('''delete from "SHOP"."ORDERS" where "ID" = '20' and "NAME" IS NULL '''
                                    '''and ROWID = 'AAAR6FAAYAAAAAOAAB';''')
    assert op == "delete" and new == {} and ora.statement_values(desc(), old) == {"id": 20, "name": None}
    _, _, new, _ = ora.parse_dml('''insert into "SHOP"."ORDERS"("ID","nick") values ('2',UNISTR('h\\00e9\\d83d\\de00'));''')
    assert ora.statement_values(desc(), new) == {"id": 2, "nick": "hé\U0001F600"}
    for bad in ("select 1 from dual", '''update "T" set "A" = ''', '''insert into "T"("A","B") values ('1');'''):
        with pytest.raises(ora.ParseError):
            ora.parse_dml(bad)


def test_statements_logged_before_a_column_was_added():
    """LogMiner names columns by position and gives values in Oracle's internal formats."""
    _, _, new, _ = ora.parse_dml(
        '''insert into "SHOP"."ORDERS"("COL 1","COL 2","COL 3","COL 4","COL 5","COL 6","COL 7","COL 8","COL 9") '''
        "values (HEXTORAW('c102'),HEXTORAW('3e611066'),HEXTORAW('6127620a63'),HEXTORAW('787e0102010101'),"
        "HEXTORAW('787e0102040506075bca00'),HEXTORAW('787e0102020506000000003c3c'),HEXTORAW('00ff'),"
        "HEXTORAW(''),HEXTORAW('bff8000000000000'));")
    row = ora.statement_values(desc(), new)
    assert row == {"id": 1, "amount": -4.85, "name": "a'b\nc", "born": dt.datetime(2026, 1, 2),
                   "seen": dt.datetime(2026, 1, 2, 3, 4, 5, 123456), "at": dt.datetime(2026, 1, 2, 1, 4, 5, tzinfo=UTC),
                   "tag": "00ff", "ratio": 1.5}
    with pytest.raises(ora.ParseError):           # a column the table no longer has
        ora.statement_values(desc(), {"COL 42": ("null",)})


def test_oracle_numbers():
    n = ora.oracle_number
    assert n(bytes.fromhex("80")) == 0
    assert n(bytes.fromhex("c102")) == 1 and n(bytes.fromhex("c10b")) == 10 and n(bytes.fromhex("c202")) == 100
    assert n(bytes.fromhex("c1021a")) == ora.Decimal("1.25") and n(bytes.fromhex("c033")) == ora.Decimal("0.5")
    assert n(bytes.fromhex("3e6466")) == -1 and n(bytes.fromhex("3e611066")) == ora.Decimal("-4.85")
    assert n(bytes.fromhex("c50d2017182d")) == 1231222344
    # As DUMP(x, 16) shows them on Oracle 23ai.
    assert n(bytes.fromhex("3d644e382966")) == ora.Decimal("-123.456") and n(bytes.fromhex("d502")) == 10 ** 40
    assert n(bytes.fromhex("405b66")) == ora.Decimal("-0.001")


def test_text_forms():
    c = {x.out: x for x in desc().columns}
    assert ora.typed(c["id"], "42") == 42 and ora.typed(c["amount"], ".5") == 0.5
    assert ora.typed(c["ratio"], "1.5E+000") == 1.5
    assert ora.typed(c["at"], "2026-01-02 03:04:05.000000 -05:30") == dt.datetime(2026, 1, 2, 8, 34, 5, tzinfo=UTC)
    assert ora.typed(ora.Col("L", "l", "TIMESTAMP(6) WITH LOCAL TIME ZONE", "tsltz", 1),
                     "2026-01-01 22:04:05.000000") == dt.datetime(2026, 1, 1, 22, 4, 5, tzinfo=UTC)
    assert ora.typed(c["tag"], "00FF") == "00ff" and ora.typed(c["name"], None) is None


def test_ddl_effects():
    e = ora.ddl_effect
    assert e("ALTER TABLE orders ADD (note VARCHAR2(20));") == "add"
    assert e('ALTER TABLE "SHOP"."ORDERS" ADD "NOTE" NUMBER;') == "add"
    assert e("ALTER TABLE orders ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS;") == "none"
    assert e("ALTER TABLE orders ADD CONSTRAINT c CHECK (id > 0);") == "none"
    assert e("TRUNCATE TABLE orders;") == "truncate"
    assert e("GRANT SELECT ON orders TO someone;") == "none"
    for ddl in ("ALTER TABLE orders DROP COLUMN note;", "ALTER TABLE orders MODIFY (note VARCHAR2(5));",
                "ALTER TABLE orders RENAME COLUMN a TO b;", "DROP TABLE orders;",
                "ALTER TABLE orders DROP SUPPLEMENTAL LOG DATA (ALL) COLUMNS;", "ALTER TABLE orders SET UNUSED (a);",
                "ALTER TABLE orders DROP PARTITION p2023;", "ALTER TABLE orders TRUNCATE PARTITION p2024;",
                "ALTER TABLE orders DROP SUBPARTITION s1;", "ALTER TABLE orders EXCHANGE PARTITION p WITH TABLE x;"):
        assert e(ddl) == "fail", ddl


def test_urls():
    s = ora.settings("oracle+oracledb://cairn:p%40ss@db.example:1522/FREEPDB1")
    assert s == {"host": "db.example", "port": 1522, "user": "cairn", "password": "p@ss", "service_name": "FREEPDB1"}
    with pytest.raises(ValueError):
        ora.settings("oracle://cairn:x@db.example:1521/")


def test_unistr():
    u = ora.unistr
    assert u("h\\00e9") == "h\u00e9" and u("\\d83d\\de00") == "\U0001F600"
    assert u("a\\\\b") == "a\\b"                       # \\ is one backslash
    assert u("\\\\0041") == "\\0041"                   # ... and what follows it is text, not an escape
    assert u("\\\\\\0041") == "\\A"
    for bad in ("\\00", "\\zzzz", "end\\"):
        with pytest.raises(ora.ParseError):
            u(bad)
    _, _, new, _ = ora.parse_dml('''insert into "SHOP"."ORDERS"("ID","nick") values ('3',UNISTR('C:\\\\dir\\00e9'));''')
    assert ora.statement_values(desc(), new)["nick"] == "C:\\dir\u00e9"


def test_numbers_keep_their_exact_values():
    """NUMBER(p,0) up to 18 digits is an integer; other NUMBERs with a precision are decimals; NUMBER alone is
    exact text. Never a float: a key above 2**53 or a money value stays exactly what it was."""
    nt = ora.number_type
    assert nt(10, 0) == ("int", None, 0) and nt(18, 0) == ("int", None, 0)
    assert nt(19, 0) == ("dec", 19, 0) and nt(20, 2) == ("dec", 20, 2) and nt(None, 0) == ("dec", 38, 0)
    assert nt(None, None) == ("numtext", None, 0) and nt(5, -2) == ("int", None, 0)
    key = ora.Col("ID", "id", "NUMBER", "dec", 1, 19, 0)
    money = ora.Col("AMT", "amt", "NUMBER", "dec", 2, 20, 2)
    free = ora.Col("Q", "q", "NUMBER", "numtext", 3)
    big = 2 ** 53 + 1
    assert ora.typed(key, str(big)) == big and ora.typed(key, str(big)) != big - 1
    assert str(ora.typed(money, "123456789012345678.91")) == "123456789012345678.91"
    assert str(ora.typed(money, "5")) == "5.00"
    assert ora.typed(free, ".5") == "0.5" and ora.typed(free, "1.0E+40") == "1" + "0" * 40
    assert ora.typed(free, "-3") == "-3" and ora.typed(free, "0") == "0"
    # The same values from the redo's internal format.
    d = desc()
    assert ora.internal(free, bytes.fromhex("c033"), d) == "0.5"
    assert ora.internal(free, bytes.fromhex("d502"), d) == "1" + "0" * 40
    assert str(ora.internal(money, bytes.fromhex("c1021a"), d)) == "1.25"
    s = ora.arrow_schema(ora.Desc("S", "T", [key, money, free], ["id"], True))
    assert [str(f.type) for f in s] == ["decimal128(19, 0)", "decimal128(20, 2)", "string"]


def test_local_time_zone_values_from_the_redo():
    """TIMESTAMP WITH LOCAL TIME ZONE is kept in the database's time zone: decoded to UTC with its offset; a
    region name cannot be (the mirror fails rather than guess)."""
    col = ora.Col("L", "l", "TIMESTAMP(6) WITH LOCAL TIME ZONE", "tsltz", 1)
    raw = bytes.fromhex("787e0102040506")                  # 2026-01-02 03:04:05 in the database's zone
    d = desc()
    d.dbtz = ora.tz_offset("+05:30")
    assert ora.internal(col, raw, d) == dt.datetime(2026, 1, 1, 21, 34, 5, tzinfo=UTC)
    d.dbtz = ora.tz_offset("-08:00")
    assert ora.internal(col, raw, d) == dt.datetime(2026, 1, 2, 11, 4, 5, tzinfo=UTC)
    d.dbtz = ora.tz_offset("Europe/Paris")
    with pytest.raises(ora.ParseError):
        ora.internal(col, raw, d)


def test_logs_must_follow_one_another():
    c = ora.contiguous
    assert c([(1, 10, 100, 200), (1, 11, 200, 300), (1, 12, 300, 2 ** 63)], 150)
    assert not c([(1, 10, 100, 200), (1, 12, 300, 400)], 150)            # sequence 11 is missing
    assert not c([(1, 10, 100, 200), (1, 11, 210, 300)], 150)            # SCNs 200 to 210 are in no file
    assert not c([(1, 11, 200, 300)], 150)                               # nothing covers 150
    assert not c([], 150)
    assert c([(1, 5, 100, 400), (2, 7, 120, 500)], 150)                  # two threads (RAC), each whole


class FakeCursor:
    """Answers the statements changes() sends from a script, and records them."""

    def __init__(self, conn):
        self.conn, self.rows, self.arraysize = conn, [], 100

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, binds=None, **kw):
        self.conn.sent.append(" ".join(sql.split()))
        self.rows = self.conn.answer(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeConn:
    def __init__(self, scns, mined, oldest=None):
        self.scns, self.mined, self.oldest, self.sent = list(scns), mined, oldest, []

    def cursor(self):
        return FakeCursor(self)

    def answer(self, sql):
        if "current_scn FROM v$database" in sql:
            return [(self.scns.pop(0),)]
        if "FROM v$transaction" in sql:
            return [self.oldest] if self.oldest else []
        if "MIN(first_change#)" in sql:
            return [(1,)]
        if "FROM v$logmnr_contents" in sql:
            return self.mined
        return []


def row(commit, op, scn=None, *values):
    return (commit, op, 0, None, "", "AAAR6FAAYAAAAAOAAA", scn or commit, *values)


def one_column():
    return ora.Desc("S", "T", [ora.Col("ID", "id", "NUMBER", "int", 1)], ["id"], True)


def test_the_recorded_position_is_what_logminer_proved():
    """A commit at or below the current SCN can still be on its way to the redo log (COMMIT WRITE NOWAIT): the
    mirror records the current SCN only once LogMiner has read a commit at or after it, else the last commit it
    read, keeping its restart point."""
    # Read: 1000 before, no open transaction, 1010 now. LogMiner reads our commit at 1004 and another at 1006.
    conn = FakeConn([1000, 1010], [row(1004, "INSERT", 1003, "7", 1, None, 0), row(1004, "COMMIT"),
                                   row(1006, "COMMIT")])
    out, end, holding = ora.changes(conn, one_column(), applied=990, restart=980)
    assert out == [(1004, "upsert", {"id": 7})] and end == (1006, 980) and holding is None
    # The reads come in the order that cannot miss a transaction: the SCN, the open transactions, the SCN.
    assert "v$database" in conn.sent[0] and "v$transaction" in conn.sent[1] and "v$database" in conn.sent[2]
    # Once a commit at or after the current SCN has been read, the position moves to it, and the restart point
    # to the SCN read before the open transactions.
    conn = FakeConn([1000, 1010], [row(1012, "COMMIT")])
    assert ora.changes(conn, one_column(), applied=990, restart=980)[1] == (1010, 1000)
    # Nothing proved beyond what was applied: nothing recorded.
    conn = FakeConn([1000, 1010], [])
    assert ora.changes(conn, one_column(), applied=990, restart=980)[1] is None


def test_a_gap_logminer_reports_fails_the_mirror():
    conn = FakeConn([1000, 1010], [row(0, "MISSING_SCN", 995)])
    with pytest.raises(RuntimeError, match="archived logs this mirror had read to are gone"):
        ora.changes(conn, one_column(), applied=990, restart=980)


def test_a_long_open_transaction_is_held_and_named(monkeypatch):
    monkeypatch.setattr(ora, "HOLD_AFTER", 60)
    conn = FakeConn([1000, 1010], [row(1011, "COMMIT")], oldest=(900, "3.14.159", 7200, b"\x01"))
    out, end, holding = ora.changes(conn, one_column(), applied=990, restart=900)
    assert end == (1010, 900)                                   # the restart point stays on its start
    assert holding.startswith("holding") and "3.14.159" in holding and "120 minutes" in holding


def test_driver_errors_while_creating_are_wrapped(monkeypatch):
    """Any driver error while checking a new mirror's table is a 502, without the URL's secrets."""
    from cairn.pipelines.mirror import MirrorError, Mirrors

    url = "oracle://cairn:S3cret-pw@db.example:1521/FREEPDB1"

    class Conn:
        def close(self):
            raise OSError("already closed")
    monkeypatch.setattr(ora, "connect", lambda u: Conn())

    def boom(conn, owner, table):
        raise ConnectionResetError(f"DPY-4011: the database closed the connection ({url}, S3cret-pw)")
    monkeypatch.setattr(ora, "check", boom)
    m = Mirrors(None, None, {"src": url}.get)
    with pytest.raises(MirrorError) as e:
        m._create_oracle("src", ["SHOP", "ORDERS"])
    assert e.value.status == 502 and "DPY-4011" in str(e.value) and "S3cret" not in str(e.value)
