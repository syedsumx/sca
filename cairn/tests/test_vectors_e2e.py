"""Vector search on lakehouse data, with the real Cairn Store and Cairn Catalog: an index over a table's
text finds rows by meaning, keeps in step with the table (re-embedding only what changed), keeps its
vectors in the lakehouse, and is searched only by those who may read the source table without a mask or
row filter. Needs the store and catalog binaries (and docker, like the rest of the lakehouse tests)."""

import pytest

pa = pytest.importorskip("pyarrow")

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")

DOCS = [("t1", "Refunds are issued within 30 days of purchase when the item is returned unused."),
        ("t2", "Shipping to Europe takes five to seven business days by standard post."),
        ("t3", "Reset your password from the account settings page using the emailed link."),
        ("t4", "Invoices can be downloaded as PDF from the billing section every month.")]


class Who:
    def __init__(self, urn):
        self.urn = urn


@pytest.fixture(scope="module")
def ix(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.ai.embeddings import HashingEmbedder
    from cairn.common.db import init_db
    from cairn.pipelines.vectors import VectorIndexes

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('vectors')}/cairn.db")
    init_db(engine)
    access = {"urn:li:corpuser:ana": (True, False), "urn:li:corpuser:bob": (True, True), "urn:li:corpuser:eve": (False, False)}
    v = VectorIndexes(engine, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                               "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                               "s3.region": "us-east-1", "s3.path-style-access": "true"},
                      HashingEmbedder(), may_read=lambda p, table: access[p.urn])
    cat = v._catalog()
    cat.create_namespace_if_not_exists("support")
    data = pa.table({"id": [d[0] for d in DOCS], "body": [d[1] for d in DOCS]})
    cat.create_table("support.articles", schema=data.schema).append(data)
    return v


def test_an_index_finds_rows_by_meaning_and_keeps_in_step(ix):
    ana = Who("urn:li:corpuser:ana")
    ix.create({"name": "articles", "source": "support.articles", "key": "id", "text": "body", "chunk_chars": 400},
              "urn:li:corpuser:admin")
    with pytest.raises(Exception, match="not been built"):
        ix.search(ana, "articles", "refund")
    assert ix.refresh("articles") == {"index": "articles", "chunks": 4, "embedded": 4, "kept": 0, "removed": 0,
                                       "method": "exact"}            # 4 chunks: under the automatic IVF threshold
    top = ix.search(ana, "articles", "how do I get my money back for a returned item", k=2)["results"]
    assert top[0]["key"] == "t1" and top[0]["score"] > top[1]["score"]
    assert ix.search(ana, "articles", "forgot password login", k=1)["results"][0]["key"] == "t3"
    # The vectors are a lakehouse table of their own.
    vt = ix._catalog().load_table(("support", "articles__articles_vectors")).scan().to_arrow()
    assert vt.num_rows == 4 and len(vt.column("embedding")[0].as_py()) == ix.embedder.dim
    # Unchanged: nothing embedded again. One row changed, one removed, one added: only those.
    assert ix.refresh("articles")["embedded"] == 0
    src = ix._catalog().load_table(("support", "articles"))
    rows = [r for r in src.scan().to_arrow().to_pylist() if r["id"] != "t4"]
    rows = [{**r, "body": "Shipping to Asia takes ten business days by air mail."} if r["id"] == "t2" else r for r in rows]
    rows.append({"id": "t5", "body": "Gift cards never expire and can be combined at checkout."})
    src.overwrite(pa.Table.from_pylist(rows, schema=src.schema().as_arrow()))
    assert ix.refresh("articles") == {"index": "articles", "chunks": 4, "embedded": 2, "kept": 2, "removed": 1,
                                       "method": "exact"}
    assert ix.search(ana, "articles", "gift card expiry", k=1)["results"][0]["key"] == "t5"
    assert all(r["key"] != "t4" for r in ix.search(ana, "articles", "invoice pdf billing", k=4)["results"])


def test_only_those_who_may_read_the_source_search(ix):
    from cairn.pipelines.pipes import PipeError

    with pytest.raises(PipeError, match="read access"):
        ix.search(Who("urn:li:corpuser:eve"), "articles", "refund")
    with pytest.raises(PipeError, match="mask or row filter"):
        ix.search(Who("urn:li:corpuser:bob"), "articles", "refund")
    with pytest.raises(PipeError, match="exists"):
        ix.create({"name": "articles", "source": "support.articles", "key": "id", "text": "body"}, "urn:li:corpuser:admin")


def test_long_text_is_chunked_with_overlap():
    from cairn.pipelines.vectors import chunks

    text = " ".join(f"word{i}" for i in range(1000))
    parts = chunks(text, 500)
    assert len(parts) > 5 and all(len(p) <= 500 for p in parts)
    assert parts[0].split()[-1] in parts[1]                                      # overlap
    assert " ".join(parts).count("word999") >= 1 and chunks("", 500) == []


def test_an_index_is_changed_and_removed_with_its_vectors(ix):
    from cairn.pipelines.pipes import PipeError
    from pyiceberg.exceptions import NoSuchTableError

    ix.create({"name": "short", "source": "support.articles", "key": "id", "text": "body", "chunk_chars": 400},
              "urn:li:corpuser:admin")
    with pytest.raises(PipeError, match="delete it and create one"):
        ix.update("short", {"source": "support.other"}, "urn:li:corpuser:admin")
    with pytest.raises(PipeError, match="every_minutes"):
        ix.update("short", {"every_minutes": 1}, "urn:li:corpuser:admin")
    got = ix.update("short", {"chunk_chars": 200, "every_minutes": 60, "enabled": False}, "urn:li:corpuser:admin")
    assert (got["chunk_chars"], got["every_minutes"], got["enabled"], got["key"], got["text"]) == (200, 60, False, "id", "body")
    assert ix.get(got["id"])["name"] == "short"                      # by its id too
    # Its vectors are a table of their own; removing the index removes them (they hold the raw text).
    ix.delete("articles", "urn:li:corpuser:admin")
    with pytest.raises(NoSuchTableError):
        ix._catalog().load_table(("support", "articles__articles_vectors"))
    with pytest.raises(PipeError, match="no such"):
        ix.get("articles")


class ClusterEmbedder:
    """Texts "c<cluster> <n>" embedded near their cluster's centre: data with the structure real embeddings have."""

    name, dim = "clusters", 48

    def __init__(self, clusters: int = 60):
        import numpy as np

        self.centres = np.random.default_rng(1).normal(size=(clusters, self.dim))

    def embed(self, texts):
        import hashlib

        import numpy as np

        out = []
        for t in texts:
            c = int(t.split()[0][1:]) if t.startswith("c") else 0
            noise = np.random.default_rng(int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)).normal(scale=0.35, size=self.dim)
            out.append((self.centres[c] + noise).astype(float).tolist())
        return out


def test_an_approximate_index_finds_nearly_what_exact_search_finds_with_far_less_work(lake, tmp_path):  # noqa: F811
    """Queue row 22: an IVF index built at refresh, stored beside the vectors, searched in its nearest lists only."""
    from sqlalchemy import create_engine, update

    from cairn.common.db import init_db
    from cairn.pipelines.pipes import PipeError
    from cairn.pipelines.vectors import VectorIndexes, vector_indexes

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    v = VectorIndexes(engine, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                               "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                               "s3.region": "us-east-1", "s3.path-style-access": "true"}, ClusterEmbedder())
    cat = v._catalog()
    cat.create_namespace_if_not_exists("ann")
    n = 6000
    rows = pa.table({"id": [f"r{i}" for i in range(n)], "body": [f"c{i % 60} {i}" for i in range(n)]})
    cat.create_table("ann.docs", schema=rows.schema).append(rows)
    with pytest.raises(PipeError, match="ann"):
        v.create({"name": "bad", "source": "ann.docs", "key": "id", "text": "body", "ann": {"lists": 1}}, "admin")
    v.create({"name": "fast", "source": "ann.docs", "key": "id", "text": "body", "ann": {"lists": 64, "probes": 6}}, "admin")
    v.create({"name": "exact", "source": "ann.docs", "key": "id", "text": "body", "ann": False}, "admin")
    assert v.refresh("fast")["method"] == "ivf" and v.refresh("exact")["method"] == "exact"
    ix = v.get("fast")
    vt = cat.load_table(("ann", "docs__fast_vectors"))
    assert ix["ann_snapshot"] == vt.current_snapshot().snapshot_id
    assert vt.io.new_input(v._ann_path(vt, ix["ann_snapshot"])).exists()          # stored beside the vectors

    hits, work = 0, []
    for q in range(30):
        text = f"c{q * 2 % 60} query {q}"
        fast, exact = v.search(None, "fast", text, 10), v.search(None, "exact", text, 10)
        assert fast["method"] == "ivf" and exact["method"] == "exact"
        hits += len({r["key"] for r in fast["results"]} & {r["key"] for r in exact["results"]})
        work.append(fast["searched"])
    recall = hits / 300
    print(f"\nrecall@10 {recall:.3f}, chunks compared per query {sum(work) / len(work):.0f} of {n}")
    assert recall >= 0.9, recall
    assert max(work) < n / 4                                # a fraction of the index, not all of it

    # A stored index for another snapshot is not used: the search is exact (and right).
    with engine.begin() as conn:
        conn.execute(update(vector_indexes).where(vector_indexes.c.name == "fast").values(ann_snapshot=1))
    v._cache.clear()
    assert v.search(None, "fast", "c3 query", 5)["method"] == "exact"
    # The next refresh after the table changes builds it again for the new snapshot.
    cat.load_table(("ann", "docs")).append(pa.table({"id": ["r-new"], "body": ["c5 new"]}))
    out = v.refresh("fast")
    assert out["method"] == "ivf" and out["embedded"] == 1
    assert v.get("fast")["ann_snapshot"] == cat.load_table(("ann", "docs__fast_vectors")).current_snapshot().snapshot_id
    assert v.search(None, "fast", "c5 new", 3)["method"] == "ivf"
    # Changing the setting to exact takes effect on the next refresh.
    v.update("fast", {"ann": False}, "admin")
    assert v.refresh("fast")["method"] == "exact" and v.search(None, "fast", "c5 new", 3)["method"] == "exact"
