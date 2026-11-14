"""Several catalog replicas on one PostgreSQL database (CAIRN_CATALOG_DATABASE_URL): every replica sees
every change at once, a commit prepared against stale metadata loses on any replica, racing creates
leave exactly one winner, and the lakehouse stays up when a replica dies. The replicas also start
together with scoped credentials on, racing to create the same store role.

Needs CAIRN_CATALOG_TEST_DATABASE_URL (a PostgreSQL server the tests may create databases on)."""

import tempfile
import threading
import uuid

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException, NoSuchTableError, TableAlreadyExistsError

from conftest import PG, Stack

pytestmark = pytest.mark.skipif(not PG, reason="needs CAIRN_CATALOG_TEST_DATABASE_URL")

STS = {"CAIRN_STORE_STS_SECRET": "catalog-sts-" + "s" * 32}
ROWS = pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array(["a", "b"], pa.string())})


@pytest.fixture(scope="module")
def ha():
    with tempfile.TemporaryDirectory() as d:
        st = Stack(d, store_env=STS, catalog_env={"CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables"}, replicas=3).start()
        yield st
        st.stop()


def test_replicas_share_one_lakehouse(ha):
    a, b = ha.catalog(replica=0), ha.catalog(replica=1)
    ns = "ha_" + uuid.uuid4().hex[:6]
    a.create_namespace(ns)
    assert (ns,) in b.list_namespaces()
    t = a.create_table(f"{ns}.t", schema=ROWS.schema)
    t.append(ROWS)
    # Written through one replica, read through another (data through scoped credentials).
    assert b.load_table(f"{ns}.t").scan().to_arrow().num_rows == 2
    b.load_table(f"{ns}.t").append(ROWS)
    assert a.load_table(f"{ns}.t").scan().to_arrow().num_rows == 4
    b.rename_table(f"{ns}.t", f"{ns}.u")
    with pytest.raises(NoSuchTableError):
        a.load_table(f"{ns}.t")
    assert a.list_tables(ns) == [(ns, "u")]


def test_a_stale_commit_loses_on_any_replica(ha, caplog):
    a, b = ha.catalog(replica=0), ha.catalog(replica=1)
    ns = "ha_" + uuid.uuid4().hex[:6]
    a.create_namespace(ns)
    a.create_table(f"{ns}.t", schema=ROWS.schema)
    ta, tb = a.load_table(f"{ns}.t"), b.load_table(f"{ns}.t")
    ta.append(ROWS)
    # tb still holds the metadata from before ta's commit: the compare-and-set refuses it on replica 1
    # (pyiceberg then reloads and tries again, on top of ta's snapshot).
    tb.append(ROWS)
    assert "concurrent update" in caplog.text
    t = a.load_table(f"{ns}.t")
    assert len(t.snapshots()) == 2 and t.scan().to_arrow().num_rows == 4


def test_racing_writers_through_every_replica(ha):
    ns = "ha_" + uuid.uuid4().hex[:6]
    ha.catalog().create_namespace(ns)
    created, errors = [], []

    def create(i):
        try:
            ha.catalog(replica=i % 3).create_table(f"{ns}.same", schema=ROWS.schema)
            created.append(i)
        except TableAlreadyExistsError:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(created) == 1, (created, errors)

    # Appends from every replica at once: each retries on conflict, and none is lost.
    done = []

    def append(i):
        cat = ha.catalog(replica=i % 3)
        for _ in range(20):
            try:
                cat.load_table(f"{ns}.same").append(ROWS)
                done.append(i)
                return
            except CommitFailedException:
                continue

    threads = [threading.Thread(target=append, args=(i,)) for i in range(9)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(done) == 9
    t = ha.catalog(replica=2).load_table(f"{ns}.same")
    assert len(t.snapshots()) == 9 and t.scan().to_arrow().num_rows == 18


def test_the_lakehouse_survives_a_replica(ha):
    ns = "ha_" + uuid.uuid4().hex[:6]
    ha.catalog(replica=0).create_namespace(ns)
    ha.catalog(replica=0).create_table(f"{ns}.t", schema=ROWS.schema).append(ROWS)
    victim = ha.replicas[0]
    victim.kill()
    victim.wait(10)
    c = ha.catalog(replica=1)
    c.load_table(f"{ns}.t").append(ROWS)
    assert ha.catalog(replica=2).load_table(f"{ns}.t").scan().to_arrow().num_rows == 4
