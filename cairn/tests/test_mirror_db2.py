"""Db2 mirrors (L167), the parts that need no database: connection URLs, positions as offsets, and how Db2's types
land. The mirror itself runs against a real Db2 in ``test_mirror_db2_e2e.py``."""

import datetime as dt
from decimal import Decimal

import pytest

from cairn.pipelines import mirror_db2 as db2


def test_urls():
    s = db2.settings("db2://cairn:p%40ss%3Bword@db.example:50001/SALES?capture_schema=asncdc")
    assert s == {"host": "db.example", "port": 50001, "user": "cairn", "password": "p@ss;word", "database": "SALES",
                 "capschema": "ASNCDC"}
    assert db2.settings("db2://u:p@h/DB")["port"] == 50000 and db2.settings("db2://u:p@h/DB")["capschema"] == "ASN"
    for bad in ("db2://u:p@h", "postgresql://u:p@h/db", "db2://u:p@h/DB?capture_schema=a;b"):
        with pytest.raises(ValueError):
            db2.settings(bad)


def test_a_password_never_goes_into_the_connection_string(monkeypatch):
    import sys
    import types

    seen = {}

    class Conn:
        def set_autocommit(self, on):
            seen["autocommit"] = on
    fake = types.SimpleNamespace(connect=lambda dsn, user, password: seen.update(dsn=dsn, user=user,
                                                                                 password=password) or Conn())
    monkeypatch.setitem(sys.modules, "ibm_db_dbi", fake)
    db2.connect("db2://cairn:se%3Bcret@db.example:50001/SALES")
    assert "se;cret" not in seen["dsn"] and "PWD" not in seen["dsn"] and seen["password"] == "se;cret"
    assert "HOSTNAME=db.example;PORT=50001" in seen["dsn"] and seen["autocommit"] is True
    with pytest.raises(ValueError):                         # a host that would add to the connection string
        db2.connect("db2://u:p@h;SECURITY=NONE/SALES")


def test_offsets_follow_commit_positions():
    a = bytes.fromhex("00000000000014D70000000000058C17")
    b = bytes.fromhex("00000000000014D70000000000058C3A")   # same log flush, a later commit
    c = bytes.fromhex("00000000000014E40000000000058C40")
    assert db2.offset(a) < db2.offset(b) < db2.offset(c)
    assert db2.seq_bytes(db2.seq_hex(a)) == a and db2.seq_hex(a) == "0x00000000000014D70000000000058C17"
    assert db2.offset(b"\xff" * 16) == 2 ** 63 - 1


def test_types():
    assert db2.kind("INTEGER", 1208)[0] == "int" and db2.kind("DECIMAL", 1208)[0] == "dec"
    assert db2.kind("VARCHAR", 0)[0] == "bin" and db2.kind("VARCHAR", 1208)[0] == "text"
    assert db2.kind("DECFLOAT", 0)[0] == "text" and db2.kind("XML", 1208)[0] == "text"
    dec = db2.Col("A", "a", "dec", 12, 2)
    assert db2.value(dec, Decimal("1.50")) == Decimal("1.50")
    assert db2.value(db2.Col("B", "b", "bin"), b"\x00\xff") == "00ff"
    assert db2.value(db2.Col("T", "t", "time"), dt.time(10, 30)) == "10:30:00"
    assert db2.value(db2.Col("D", "d", "text"), Decimal("1E+400")) == "1E+400"
    desc = db2.Desc(schema="S", table="T", capschema="ASN", key=["ID"], cd_owner="S", cd_table="CDT", before={},
                    del_ins=False, state="A",
                    columns=[db2.Col("ID", "id", "int"), dec, db2.Col("WHEN", "when", "ts"),
                             db2.Col("DAY", "day", "date")])
    import pyarrow as pa

    assert db2.arrow_schema(desc) == pa.schema([pa.field("id", pa.int64()), pa.field("a", pa.decimal128(12, 2)),
                                                pa.field("when", pa.timestamp("us")), pa.field("day", pa.date32())])
    assert desc.out_key == ["id"] and db2.map_id("0123456789abcdef") == "C012345678"
