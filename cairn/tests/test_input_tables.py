"""Input tables: values typed into a table become typed SQL literals or are refused (the writes themselves run on
real Trino, as the person, in tests/test_trino_e2e.py)."""

import pytest

from cairn.sqlworkspace.input_tables import InputTableError, stamp_literal, typed_literal


def test_values_become_typed_literals():
    assert typed_literal("EU", "text", "region") == "'EU'"
    assert typed_literal("x'); DROP TABLE t; --", "text", "c") == "'x''); DROP TABLE t; --'"
    assert typed_literal(1200.5, "number", "t") == "CAST(1200.5 AS double)"
    assert typed_literal("300", "number", "t") == "CAST(300.0 AS double)"
    assert typed_literal("42", "integer", "n") == "CAST(42 AS bigint)"
    assert typed_literal(True, "boolean", "b") == "TRUE" and typed_literal("false", "boolean", "b") == "FALSE"
    assert typed_literal("2026-12-31", "date", "d") == "DATE '2026-12-31'"
    assert typed_literal(None, "number", "t") == "CAST(NULL AS double)" and typed_literal("", "date", "d") == "CAST(NULL AS date)"
    assert typed_literal("2026-11-04T10:00:00.123456+02:00", "timestamp", "at") == "TIMESTAMP '2026-11-04 08:00:00.123456 UTC'"
    # A value read back from Trino (str of a datetime) compares equal to itself, microseconds kept.
    assert stamp_literal("2026-11-04 10:00:00.120000+00:00") == "TIMESTAMP '2026-11-04 10:00:00.120000 UTC'"


@pytest.mark.parametrize("value,kind", [("lots", "number"), ("1); DROP", "number"), (True, "number"), ("1.5", "integer"),
                                        (float("nan"), "number"), ("maybe", "boolean"), ("tomorrow", "date"),
                                        ("2026-11-04T10:00:00", "timestamp"), ({"a": 1}, "text"), (True, "text"),
                                        ("x" * 10_001, "text")])
def test_values_that_are_not_of_the_type_are_refused(value, kind):
    with pytest.raises(InputTableError):
        typed_literal(value, kind, "c")
