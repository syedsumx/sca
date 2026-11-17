"""Approximate (IVF) vector search, without the store and catalog: the search over a given index, its settings, and
the index files it keeps beside the vectors."""

import pytest

np = pytest.importorskip("numpy")

from cairn.pipelines.pipes import PipeError  # noqa: E402
from cairn.pipelines.vectors import VectorIndexes  # noqa: E402


class Fixed:
    name, dim = "fixed", 2

    def __init__(self, v):
        self.v = v

    def embed(self, texts):
        return [self.v for _ in texts]


def _index(ann_lists, ann_probes, m, meta, ivf):
    v = VectorIndexes(None, {}, Fixed([1.0, 0.0]))
    ix = {"id": "i1", "name": "docs", "source": "s.t", "refreshed_ms": 1, "ann_lists": ann_lists,
          "ann_probes": ann_probes, "ann_snapshot": 5}
    v.get = lambda name: ix
    v._matrix = lambda ix: (m, meta, ivf)
    return v


def test_an_ivf_search_returns_k_results_when_the_probed_lists_hold_fewer():
    """With many small lists (lists close to the chunk count) and few probes, the probed lists held fewer than k
    chunks and the search returned fewer results than asked for, where exact search returns k: an answer then drew on
    one passage. It now probes further lists, nearest first, until it has k candidates."""
    angles = np.linspace(0, np.pi / 2, 16)
    m = np.stack([np.cos(angles), np.sin(angles)], axis=1).astype(np.float32)
    meta = [{"key": f"r{i}", "chunk": 0, "text": f"t{i}"} for i in range(16)]
    centres = m[::2].copy()                                         # 8 lists of 2 chunks each
    members = {j: np.array([2 * j, 2 * j + 1]) for j in range(8)}
    v = _index(8, 1, m, meta, (centres, members))
    out = v.search(None, "docs", "q", 5)
    assert out["method"] == "ivf"
    assert [r["key"] for r in out["results"]] == ["r0", "r1", "r2", "r3", "r4"]      # the exact top 5
    assert out["searched"] < 16                                     # still a fraction of the index


def test_two_indexes_never_share_a_vectors_table(tmp_path):
    """The vectors table is named from the index's name, lowercased, other characters as ``_``: indexes "docs" and
    "Docs", or "a-b" and "a_b", over one table were both accepted and wrote the same vectors table, so refreshing one
    replaced the other's chunks (with another text column's, possibly), and deleting one purged both."""
    from cairn.common.db import get_engine, init_db

    engine = get_engine(f"sqlite:///{tmp_path}/v.db")
    init_db(engine)
    v = VectorIndexes(engine, {}, Fixed([1.0, 0.0]))
    v.create({"name": "docs", "source": "s.t", "key": "id", "text": "body"}, "urn:li:corpuser:a")
    v.create({"name": "a-b", "source": "s.t", "key": "id", "text": "body"}, "urn:li:corpuser:a")
    for name in ("Docs", "a_b", "A.B"):
        with pytest.raises(PipeError) as e:
            v.create({"name": name, "source": "s.t", "key": "id", "text": "notes"}, "urn:li:corpuser:b")
        assert e.value.status == 409 and "vectors table" in str(e.value)
    with pytest.raises(PipeError) as e:                       # the same name again: it exists (whatever the table)
        v.create({"name": "docs", "source": "s.t", "key": "id", "text": "body"}, "urn:li:corpuser:a")
    assert e.value.status == 409 and "exists" in str(e.value)
    v.create({"name": "Docs", "source": "s.u", "key": "id", "text": "body"}, "urn:li:corpuser:b")   # another table's
    assert sorted(v.target(ix) for ix in v.list()) == ["s.t__a_b_vectors", "s.t__docs_vectors", "s.u__docs_vectors"]


def test_ann_settings_default_their_probes_within_their_lists():
    """`probes` defaults to 8: an index of fewer lists given without probes was refused with "probes: 1-lists"
    although the caller never set probes."""
    assert VectorIndexes._ann({"lists": 4}) == (4, 4)
    assert VectorIndexes._ann({"lists": 256}) == (256, 8)
    assert VectorIndexes._ann({"lists": 16, "probes": 3}) == (16, 3)
    assert VectorIndexes._ann(False) == (0, None) and VectorIndexes._ann("auto") == (None, None)
    for bad in [{"lists": 1}, {"lists": 4, "probes": 5}, {"lists": 4, "probes": 0}, {"lists": True}, {"lists": 70000},
                {"probes": 2}, "fast", 3]:
        with pytest.raises(PipeError):
            VectorIndexes._ann(bad)


class _Out:
    def __init__(self, files, path):
        self.files, self.path = files, path

    def create(self, overwrite=False):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        self.files[self.path] = data


class _In:
    def __init__(self, files, path):
        self.files, self.path = files, path

    def exists(self):
        return self.path in self.files


class _IO:
    def __init__(self):
        self.files = {}

    def new_output(self, path):
        return _Out(self.files, path)

    def new_input(self, path):
        return _In(self.files, path)

    def delete(self, path):
        self.files.pop(path, None)


class _Snap:
    def __init__(self, sid):
        self.snapshot_id = sid


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def to_arrow(self):
        return self

    def to_pylist(self):
        return self.rows


class _Vectors:
    """A vectors table: its location, current snapshot, rows and FileIO."""

    def __init__(self, sid, n=40):
        rng = np.random.default_rng(1)
        self.io, self.sid = _IO(), sid
        self.rows = [{"key": f"r{i}", "chunk": 0, "embedding": rng.normal(size=4).tolist()} for i in range(n)]

    def location(self):
        return "s3://lake/s/t__docs_vectors"

    def current_snapshot(self):
        return _Snap(self.sid)

    def scan(self, selected_fields=()):
        return _Rows(self.rows)


def test_a_rebuilt_ivf_index_deletes_the_one_it_replaces():
    """Each refresh that changed the vectors wrote <location>/cairn-ann/<snapshot>.npz and left the previous ones:
    files no snapshot references, holding the row keys of rows long since deleted from the source, piling up beside
    the table for as long as it exists."""
    v = VectorIndexes(None, {}, Fixed([1.0, 0.0]))
    vt = _Vectors(sid=11)
    ix = {"id": "i1", "name": "docs", "ann_lists": 4, "ann_probes": 2, "ann_snapshot": None}
    assert v._build_ann(ix, vt, len(vt.rows)) == {"ann_snapshot": 11}
    assert list(vt.io.files) == [v._ann_path(vt, 11)]
    vt.sid = 12                                                    # the vectors changed: built again
    assert v._build_ann({**ix, "ann_snapshot": 11}, vt, len(vt.rows)) == {"ann_snapshot": 12}
    assert list(vt.io.files) == [v._ann_path(vt, 12)]
    # Turned off: the last one goes too.
    assert v._build_ann({**ix, "ann_lists": 0, "ann_snapshot": 12}, vt, len(vt.rows)) == {"ann_snapshot": None}
    assert vt.io.files == {}


def test_a_stored_ivf_index_that_went_missing_is_built_again():
    """The index file sits under the table's location but no snapshot references it, so orphan-file removal (a
    maintenance policy's orphan_days) deletes it. The refresh only checked the recorded snapshot, so until the source
    changed again every search was exact, however large the index."""
    v = VectorIndexes(None, {}, Fixed([1.0, 0.0]))
    vt = _Vectors(sid=11)
    ix = {"id": "i1", "name": "docs", "ann_lists": 4, "ann_probes": 2, "ann_snapshot": None}
    assert v._build_ann(ix, vt, len(vt.rows)) == {"ann_snapshot": 11}
    assert v._build_ann({**ix, "ann_snapshot": 11}, vt, len(vt.rows)) == {}        # there: nothing to do
    vt.io.files.clear()                                                            # removed as an orphan
    assert v._build_ann({**ix, "ann_snapshot": 11}, vt, len(vt.rows)) == {"ann_snapshot": 11}
    assert list(vt.io.files) == [v._ann_path(vt, 11)]


def test_changing_an_indexs_ann_setting_deletes_its_stored_ivf_index(tmp_path):
    """A change of `ann` forgets the stored index (it is built again with the new shape): its file goes with it."""
    from sqlalchemy import create_engine, update

    from cairn.common.db import init_db
    from cairn.pipelines.vectors import vector_indexes

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    v = VectorIndexes(engine, {}, Fixed([1.0, 0.0]))
    vt = _Vectors(sid=11)
    v._catalog = lambda: type("Cat", (), {"load_table": lambda self, ident: vt})()
    v.create({"name": "docs", "source": "s.t", "key": "id", "text": "body", "ann": {"lists": 4}}, "admin")
    out = v._build_ann(v.get("docs"), vt, len(vt.rows))
    with engine.begin() as conn:
        conn.execute(update(vector_indexes).where(vector_indexes.c.name == "docs").values(**out))
    assert list(vt.io.files) == [v._ann_path(vt, 11)]
    assert v.update("docs", {"ann": False}, "admin")["ann_snapshot"] is None
    assert vt.io.files == {}
