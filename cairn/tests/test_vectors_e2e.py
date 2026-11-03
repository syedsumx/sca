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
    assert ix.refresh("articles") == {"index": "articles", "chunks": 4, "embedded": 4, "kept": 0, "removed": 0}
    top = ix.search(ana, "articles", "how do I get my money back for a returned item", k=2)["results"]
    assert top[0]["key"] == "t1" and top[0]["score"] > top[1]["score"]
    assert ix.search(ana, "articles", "forgot password login", k=1)["results"][0]["key"] == "t3"
    # The vectors are a lakehouse table of their own.
    vt = ix._catalog().load_table(("support", "articles__vectors")).scan().to_arrow()
    assert vt.num_rows == 4 and len(vt.column("embedding")[0].as_py()) == ix.embedder.dim
    # Unchanged: nothing embedded again. One row changed, one removed, one added: only those.
    assert ix.refresh("articles")["embedded"] == 0
    src = ix._catalog().load_table(("support", "articles"))
    rows = [r for r in src.scan().to_arrow().to_pylist() if r["id"] != "t4"]
    rows = [{**r, "body": "Shipping to Asia takes ten business days by air mail."} if r["id"] == "t2" else r for r in rows]
    rows.append({"id": "t5", "body": "Gift cards never expire and can be combined at checkout."})
    src.overwrite(pa.Table.from_pylist(rows, schema=src.schema().as_arrow()))
    assert ix.refresh("articles") == {"index": "articles", "chunks": 4, "embedded": 2, "kept": 2, "removed": 1}
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
