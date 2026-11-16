"""A files pipe's AI enrichment charges the pipe's owner for that file's model calls, and nobody else's: the actor
is set for the read and put back after it (the per-person allowance and usage are by actor)."""

from cairn.ai.usage import current_actor, set_actor
from cairn.pipelines.pipes import Pipes


def test_a_files_pipe_charges_its_owner_only_while_reading_the_file(tmp_path, monkeypatch):
    from pyarrow import fs as pafs

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("hello")
    p = Pipes.__new__(Pipes)
    p.llm = None
    monkeypatch.setattr(p, "_fs", lambda: pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem()))
    seen = []

    def enrich(llm, enrichment, key, data, text):
        seen.append(current_actor())                             # who the model call is charged to
        return {"description": "d", "labels": None, "extracted": None}, None
    monkeypatch.setattr("cairn.pipelines.enrich.enrich", enrich)
    set_actor("urn:li:corpuser:bob")                          # whoever was acting before (a request, a tick)
    try:
        t = p._read("files", "docs/a.txt", {"describe": True}, "urn:li:corpuser:ana")
        assert t.num_rows == 1 and seen == ["urn:li:corpuser:ana"]
        assert current_actor() == "urn:li:corpuser:bob"       # later calls are not charged to the pipe's owner
    finally:
        set_actor(None)
