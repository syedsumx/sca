"""SAP HANA mirrors (L167), the parts that need no database: URLs, types, the change table's key columns and the
names of what a mirror makes on the source. The mirror itself runs against HANA in ``test_mirror_hana_e2e.py``."""

import datetime as dt
from decimal import Decimal

import pytest

from cairn.pipelines import mirror_hana as hana


def test_urls():
    s = hana.settings("hana://cairn:p%40ss@db.example:443?encrypt=true")
    assert s == {"host": "db.example", "port": 443, "user": "cairn", "password": "p@ss", "database": None,
                 "encrypt": True, "validate": True}
    assert hana.settings("hana://u:p@h:39013/HXE?encrypt=true&validate=false")["database"] == "HXE"
    assert hana.settings("hana://u:p@h")["port"] == 39041 and hana.settings("hana://u:p@h")["encrypt"] is False
    for bad in ("postgresql://u:p@h/db", "hana://u:p@h/a;b"):
        with pytest.raises(ValueError):
            hana.settings(bad)


def test_types():
    assert hana.kind("DECIMAL", 12, 2) == ("dec", 12, 2)
    assert hana.kind("DECIMAL", 34, None)[0] == "text"            # a floating decimal: its exact text
    assert hana.kind("SECONDDATE", 19, 0)[0] == "ts" and hana.kind("NCLOB", 0, None)[0] == "text"
    assert hana.sqltype("NVARCHAR", 20, None) == "NVARCHAR(20)" and hana.sqltype("DECIMAL", 12, 2) == "DECIMAL(12,2)"
    assert hana.sqltype("BIGINT", 19, 0) == "BIGINT"
    assert hana.value(hana.Col("T", "t", "time", "TIME"), dt.time(10, 30)) == "10:30:00"
    assert hana.value(hana.Col("B", "b", "bin", "VARBINARY(2)"), memoryview(b"\x00\xff")) == "00ff"
    assert hana.value(hana.Col("F", "f", "text", "DECIMAL"), Decimal("1E+40")) == "1E+40"


def test_names_and_what_is_left_behind():
    ct, triggers = hana.names("0123456789abcdef")
    assert ct == "CAIRN_CT_0123456789ABCDEF" and triggers[1] == "CAIRN_0123456789ABCDEF_U"
    left = hana.left_behind("APP", "CAIRN", "0123456789abcdef")
    assert left.count("DROP TRIGGER") == 3 and 'DROP TABLE "CAIRN"."CAIRN_CT_0123456789ABCDEF"' in left
    assert hana.q('a"b') == '"a""b"'
