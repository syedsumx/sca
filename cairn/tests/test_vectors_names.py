"""Vector index names: two indexes never share a vectors table."""

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.pipes import PipeError
from cairn.pipelines.vectors import VectorIndexes


def test_names_that_map_to_the_same_vectors_table_are_refused(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    init_db(engine)
    ix = VectorIndexes(engine, {}, embedder=None)
    a = ix.create({"name": "docs", "source": "kb.articles", "key": "id", "text": "body"}, "urn:li:corpuser:alice")
    ix.create({"name": "q-and-a", "source": "kb.articles", "key": "id", "text": "body"}, "urn:li:corpuser:alice")
    for clash in ("Docs", "DOCS", "q.and.a", "Q_AND_A"):
        with pytest.raises(PipeError) as e:
            ix.create({"name": clash, "source": "kb.articles", "key": "id", "text": "body"}, "urn:li:corpuser:mallory")
        assert e.value.status == 409
    assert [i["name"] for i in ix.list()] == ["docs", "q-and-a"] and ix.target(a) == "kb.articles__docs_vectors"
    # The same name on another table, and a different name on this one, are fine.
    ix.create({"name": "Docs", "source": "kb.other", "key": "id", "text": "body"}, "urn:li:corpuser:bob")
    ix.create({"name": "docs2", "source": "kb.articles", "key": "id", "text": "body"}, "urn:li:corpuser:bob")
