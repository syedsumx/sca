"""Serving tables: callers' values as column values, without a database (review finding on L103; the tables are tested
end to end in tests/test_serving_tables_e2e.py)."""

from __future__ import annotations

import pytest

from cairn.dataaccess.serving import ServingError, to_python


@pytest.mark.parametrize("value", [2 ** 63, -2 ** 63 - 1, "99999999999999999999"])
def test_a_long_out_of_range_is_refused(value):
    """Review finding: a key or filter value beyond a 64-bit integer reached PostgreSQL, which failed the statement:
    a write was a 500, a read a misleading 503 (or "your row filter could not be applied")."""
    with pytest.raises(ServingError) as e:
        to_python(value, "long", "id")
    assert e.value.status == 400


def test_longs_in_range_still_pass():
    assert to_python(2 ** 63 - 1, "long", "id") == 2 ** 63 - 1
    assert to_python("-9223372036854775808", "bigint", "id") == -2 ** 63
