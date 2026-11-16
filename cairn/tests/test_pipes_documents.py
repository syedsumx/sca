"""Files pipes without the store: document extraction limits and which events a pipe takes."""

import io
import zipfile

from cairn.pipelines import documents
from cairn.pipelines.pipes import Pipes


def _docx(body: bytes, member: str = "word/document.xml") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, body)
    return out.getvalue()


def test_docx_text():
    text, _, error = documents.extract("a.docx", _docx(b"<w:p><w:t>Board memo</w:t></w:p>"))
    assert text == "Board memo" and error is None


def test_docx_that_inflates_past_the_cap_is_not_read(monkeypatch):
    monkeypatch.setenv("CAIRN_PIPES_MAX_UNZIPPED_BYTES", "100000")
    bomb = _docx(b"<w:p><w:t>" + b"A" * 5_000_000 + b"</w:t></w:p>")
    assert len(bomb) < 100_000                       # small on disk
    text, _, error = documents.extract("bomb.docx", bomb)
    assert text is None and "CAIRN_PIPES_MAX_UNZIPPED_BYTES" in error
    slides = _docx(b"<a:p><a:t>" + b"A" * 5_000_000 + b"</a:t></a:p>", "ppt/slides/slide1.xml")
    text, _, error = documents.extract("bomb.pptx", slides)
    assert text is None and error


def test_events_load_only_keys_in_the_pipes_folder(monkeypatch):
    p = Pipes.__new__(Pipes)
    pipe = {"id": "1", "name": "docs", "bucket": "landing", "prefix": "docs", "format": "files", "enabled": True}
    loaded, forgotten = [], []
    monkeypatch.setattr(p, "list", lambda: [pipe])
    monkeypatch.setattr(p, "load", lambda pp, key, v: loaded.append(key) or {"key": key})
    monkeypatch.setattr(p, "forget", lambda pp, key: forgotten.append(key) or True)

    class Info:
        from pyarrow import fs as _fs
        type = _fs.FileType.File
        size, mtime_ns = 1, 1

    monkeypatch.setattr(p, "_fs", lambda: type("FS", (), {"get_file_info": lambda self, path: Info()})())

    def ev(name, key):
        return {"Records": [{"eventName": name, "s3": {"bucket": {"name": "landing"}, "object": {"key": key}}}]}

    for key in ("docs/a.txt", "docs-private/b.txt", "docsx"):
        p.on_events(ev("ObjectCreated:Put", key))
        p.on_events(ev("ObjectRemoved:Delete", key))
    assert loaded == ["docs/a.txt"] and forgotten == ["docs/a.txt"]
