"""Sample datasets never write over or purge a tenant's own tables of the same name (samples.customers, ...). The
catalog is an in-memory fake with pyiceberg's calls; loading into the real store and catalog is in
tests/test_trino_e2e.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.samples import SampleError, Samples

ADMIN = "urn:li:corpuser:admin"


class FakeTable:
    def __init__(self, columns, properties=None):
        self.columns, self.properties, self.rows = list(columns), dict(properties or {}), None

    def schema(self):
        return SimpleNamespace(fields=[SimpleNamespace(name=c) for c in self.columns])

    def overwrite(self, data):
        self.rows, self.columns = data.num_rows, list(data.column_names)

    def transaction(self):
        table = self

        class Tx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def set_properties(self, props=None, **kw):
                table.properties.update(props or {}, **kw)
        return Tx()


class FakeCatalog:
    def __init__(self):
        self.tables: dict[tuple, FakeTable] = {}

    def load_table(self, ident):
        from pyiceberg.exceptions import NoSuchTableError

        if tuple(ident) not in self.tables:
            raise NoSuchTableError(str(ident))
        return self.tables[tuple(ident)]

    def create_namespace(self, ns):
        pass

    def create_table(self, ident, schema, properties=None, **_):
        self.tables[tuple(ident)] = FakeTable(schema.names, properties)
        return self.tables[tuple(ident)]

    def purge_table(self, ident):
        from pyiceberg.exceptions import NoSuchTableError

        if self.tables.pop(tuple(ident), None) is None:
            raise NoSuchTableError(str(ident))


@pytest.fixture()
def samples(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(e)
    s = Samples(e, {"uri": "http://catalog.invalid"})
    s.cat = FakeCatalog()
    s._catalog = lambda: s.cat
    return s


def test_a_tenants_own_table_of_the_same_name_is_never_overwritten_or_purged(samples):
    theirs = FakeTable(["id", "customer_name", "lifetime_value"])     # the tenant's real samples.customers
    theirs.rows = 50_000
    samples.cat.tables[("samples", "customers")] = theirs

    with pytest.raises(SampleError) as e:
        samples.load(ADMIN, "retail")
    assert e.value.status == 409 and "samples.customers" in str(e.value)
    assert theirs.rows == 50_000 and theirs.columns == ["id", "customer_name", "lifetime_value"]
    assert set(samples.cat.tables) == {("samples", "customers")}       # nothing written before the refusal

    out = samples.remove(ADMIN, "retail")
    assert out["tables_dropped"] == [] and out["tables_kept"] == ["customers"]
    assert samples.cat.tables[("samples", "customers")] is theirs


def test_the_sets_own_tables_are_replaced_and_removed(samples):
    first = samples.load(ADMIN, "retail")
    assert first["rows"]["orders"] == 20_000
    assert all(t.properties.get("cairn.sample-set") == "retail" for t in samples.cat.tables.values())
    samples.load(ADMIN, "retail")                                         # a reload replaces its own rows
    assert samples.remove(ADMIN, "retail")["tables_dropped"] == ["customers", "products", "orders"]
    assert samples.cat.tables == {}


def test_tables_loaded_before_they_were_marked_are_still_the_sets(samples):
    from cairn.pipelines.samples import DOCS

    samples._set_state("retail", "loaded", ADMIN, {})
    for table, (_desc, cols) in DOCS.items():                             # as an earlier version left them
        samples.cat.tables[("samples", table)] = FakeTable(list(cols))
    samples.load(ADMIN, "retail")
    assert all(t.properties.get("cairn.sample-set") == "retail" for t in samples.cat.tables.values())
