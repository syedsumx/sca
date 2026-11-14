"""Chaos: Cairn Catalog replicas on PostgreSQL, with Cairn Store, while writers commit.

* A replica is killed (SIGKILL) in the middle of concurrent commits: writers fail over to the others, and
  every commit that was acknowledged is in the table exactly once (a client that is not told the outcome
  checks before retrying, as a correct client does).
* PostgreSQL restarts under the replicas: they serve again without being restarted themselves.
* The store is killed and started again: commits fail while it is down, nothing half-written is left in
  the table, and writes resume.

Needs CAIRN_CATALOG_TEST_DATABASE_URL (a PostgreSQL server the tests may create databases on); the restart
test also needs CAIRN_CHAOS_PG_CONTAINER (the docker container that server runs in)."""

import os
import subprocess
import tempfile
import threading
import time
import uuid

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException

from conftest import PG, Stack

pytestmark = pytest.mark.skipif(not PG, reason="needs CAIRN_CATALOG_TEST_DATABASE_URL")
SCHEMA = pa.schema([("batch", pa.string()), ("n", pa.int64())])


@pytest.fixture
def chaos():
    with tempfile.TemporaryDirectory() as d:
        # The store as in production: fsync on (with it off, which only tests use, a crash loses writes).
        st = Stack(d, store_env={"CAIRN_STORE_FSYNC": "true"}, replicas=3).start()
        yield st
        for p in st.procs:
            if p.poll() is None:
                p.terminate()
                p.wait(15)


def batches(table) -> list[str]:
    return table.refresh().scan(selected_fields=("batch",)).to_arrow().column("batch").to_pylist()


def append_once(stack, ns, batch: str, replicas: list[int], deadline: float) -> bool:
    """Appends ``batch`` exactly once, trying replica after replica; False if it could not by the deadline."""
    i = 0
    while time.time() < deadline:
        r = replicas[i % len(replicas)]
        i += 1
        try:
            t = stack.catalog(replica=r).load_table(f"{ns}.events")
            if batch in batches(t):                  # an earlier try landed though its answer was lost
                return True
            t.append(pa.table({"batch": [batch] * 3, "n": [1, 2, 3]}, schema=SCHEMA))
            return True
        except CommitFailedException:
            continue                                  # someone else committed first: reload and retry
        except Exception:  # noqa: BLE001 - a dead replica or a dropped connection: the next replica
            time.sleep(0.2)
    return False


def test_a_replica_killed_mid_commit_loses_nothing(chaos):
    ns = "chaos_" + uuid.uuid4().hex[:6]
    cat = chaos.catalog(replica=1)
    cat.create_namespace(ns)
    cat.create_table(f"{ns}.events", schema=SCHEMA)
    acked: list[str] = []
    lock = threading.Lock()
    deadline = time.time() + 120

    def writer(w):
        for k in range(8):
            b = f"w{w}-{k}"
            if append_once(chaos, ns, b, [w % 3, (w + 1) % 3, (w + 2) % 3], deadline):
                with lock:
                    acked.append(b)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    chaos.replicas[0].kill()                          # in the middle of the commits
    for t in threads:
        t.join(180)
    assert len(acked) == 48, f"{48 - len(acked)} writes never succeeded"
    got = batches(chaos.catalog(replica=2).load_table(f"{ns}.events"))
    assert sorted(set(got)) == sorted(acked)          # nothing lost
    assert len(got) == 3 * len(acked)                 # nothing twice
    snaps = chaos.catalog(replica=1).load_table(f"{ns}.events").snapshots()
    assert len(snaps) == 48                           # one snapshot per acknowledged commit


@pytest.mark.skipif(not os.environ.get("CAIRN_CHAOS_PG_CONTAINER"), reason="needs CAIRN_CHAOS_PG_CONTAINER")
def test_the_replicas_ride_out_a_database_restart(chaos):
    ns = "chaos_" + uuid.uuid4().hex[:6]
    chaos.catalog(replica=0).create_namespace(ns)
    chaos.catalog(replica=0).create_table(f"{ns}.events", schema=SCHEMA)
    subprocess.run(["docker", "restart", "-t", "0", os.environ["CAIRN_CHAOS_PG_CONTAINER"]], check=True, capture_output=True)
    # Every replica serves again on its own (it reconnects); no replica is restarted.
    for r in range(3):
        assert append_once(chaos, ns, f"after-{r}", [r], time.time() + 60), f"replica {r} did not recover"
    assert sorted(batches(chaos.catalog(replica=1).load_table(f"{ns}.events"))) == \
        sorted([f"after-{r}" for r in range(3) for _ in range(3)])
    assert all(p.poll() is None for p in chaos.replicas)


def test_a_store_crash_leaves_no_half_written_commit(chaos):
    ns = "chaos_" + uuid.uuid4().hex[:6]
    cat = chaos.catalog(replica=0)
    cat.create_namespace(ns)
    t = cat.create_table(f"{ns}.events", schema=SCHEMA)
    t.append(pa.table({"batch": ["before"] * 3, "n": [1, 2, 3]}, schema=SCHEMA))
    stop, failed, done = threading.Event(), [], []

    def writer():
        k = 0
        while not stop.is_set():
            b = f"during-{k}"
            k += 1
            try:
                chaos.catalog(replica=k % 3).load_table(f"{ns}.events").append(
                    pa.table({"batch": [b] * 3, "n": [1, 2, 3]}, schema=SCHEMA))
                done.append(b)
            except Exception:  # noqa: BLE001 - the store is down
                failed.append(b)
                time.sleep(0.1)

    w = threading.Thread(target=writer)
    w.start()
    time.sleep(1)
    chaos.restart_store(kill=True, down=3)            # SIGKILL mid-write, down 3 s, then back on its data
    time.sleep(2)
    stop.set()
    w.join(60)
    assert failed, "the outage was not seen"
    # The table reads in full: every snapshot's files exist, and acknowledged commits are all there.
    got = batches(chaos.catalog(replica=1).load_table(f"{ns}.events"))
    assert "before" in got and set(done) <= set(got)
    assert all(got.count(b) == 3 for b in set(got))
    # Writes resume.
    assert append_once(chaos, ns, "after", [0, 1, 2], time.time() + 30)
