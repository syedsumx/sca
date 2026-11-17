"""A stream worker's rebalance (cairn.pipelines.streams.StreamWorker): when a partition is assigned, the worker seeks
to the next offset the table's own snapshots record, so a crash between the table commit and Kafka's offset commit
writes nothing twice. kafka-python runs the rebalance listener inside ``poll`` and logs and swallows what it raises
(``ConsumerCoordinator._on_join_complete``); if the table could not be read then (the catalog restarting), the
consumer would carry on from Kafka's older committed offset and append those rows again. The worker must instead
drop what it read, show the error and start over.

The consumer here is a stand-in that behaves as kafka-python's does around the listener; the end-to-end tests with a
real broker are in tests/test_streams_e2e.py."""

import threading
import time
import types

import pytest

kafka = pytest.importorskip("kafka")

from cairn.pipelines.streams import StreamWorker  # noqa: E402

TP = kafka.TopicPartition("orders", 0)


class FakeConsumer:
    """Kafka's committed offset is 0; the table has rows up to offset 2 (next: 3), written before a crash."""

    def __init__(self, **kw):
        self.listener, self.position_ = None, 0
        self.assigned = False

    def subscribe(self, topics, listener=None):
        self.listener = listener

    def poll(self, timeout_ms=0, max_records=None):
        if not self.assigned:
            self.assigned = True
            try:                                   # as kafka-python does: the listener's error is only logged
                self.listener.on_partitions_assigned({TP})
            except Exception:  # noqa: BLE001
                pass
        msgs = [types.SimpleNamespace(partition=0, offset=o, key=None, value=b'{"id": %d}' % o)
                for o in range(self.position_, 3)]
        self.position_ = 3
        return {TP: msgs} if msgs else {}

    def seek(self, tp, offset):
        self.position_ = offset

    def commit(self, offsets):
        pass

    def end_offsets(self, tps):
        return {tp: 3 for tp in tps}

    def position(self, tp):
        return self.position_

    def close(self, autocommit=False):
        pass


class Streams:
    def __init__(self):
        self.kafka, self.written, self.status = {}, [], []

    def offsets(self, stream):
        raise ConnectionError("the catalog is restarting")

    def write(self, stream, batch):
        self.written.extend(m.offset for m in batch)
        return len(batch)

    def _status(self, sid, **values):
        self.status.append(values)


def test_a_table_that_cannot_be_read_at_assignment_writes_nothing_again(monkeypatch):
    monkeypatch.setattr(kafka, "KafkaConsumer", FakeConsumer)
    s = Streams()
    w = StreamWorker(s, {"id": "s1", "name": "orders", "topic": "orders", "start": "earliest", "batch_rows": 3,
                         "batch_seconds": 1})
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    end = time.time() + 5
    while not s.status and time.time() < end:
        time.sleep(0.05)
    w.stop.set()
    t.join(10)
    assert s.written == [], "rows the table already holds were written again from Kafka's older offset"
    assert any(v.get("status") == "error" and "catalog is restarting" in (v.get("error") or "") for v in s.status)
