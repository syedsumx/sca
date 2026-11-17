"""Approximate (IVF) vector search, without the store and catalog: the search over a given index, its settings, and
the index files it keeps beside the vectors."""

import pytest

np = pytest.importorskip("numpy")

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
