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
                "ALTER TABLE orders DROP SUPPLEMENTAL LOG DATA (ALL) COLUMNS;", "ALTER TABLE orders SET UNUSED (a);"):
        assert e(ddl) == "fail", ddl


def test_urls():
    s = ora.settings("oracle+oracledb://cairn:p%40ss@db.example:1522/FREEPDB1")
    assert s == {"host": "db.example", "port": 1522, "user": "cairn", "password": "p@ss", "service_name": "FREEPDB1"}
    with pytest.raises(ValueError):
        ora.settings("oracle://cairn:x@db.example:1521/")
