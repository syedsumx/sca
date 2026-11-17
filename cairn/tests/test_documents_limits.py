"""Text from documents (cairn.pipelines.documents): what a files pipe reads out of Word and PowerPoint files stays
within its limits, however small the file."""

import io
import zipfile

from cairn.pipelines.documents import extract


def _docx(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_a_zip_bomb_document_is_not_expanded(monkeypatch):
    """A .docx is a zip: a few KB can hold gigabytes of XML. The files pipe checks the file's size (50 MB), then read
    each member whole into memory, so a small upload could exhaust the pipe's process. Members are now counted as
    they are read, and a document that expands past CAIRN_PIPES_MAX_EXPANDED_BYTES is listed without its text."""
    monkeypatch.setenv("CAIRN_PIPES_MAX_EXPANDED_BYTES", str(1024 * 1024))
    para = b"<w:p><w:r><w:t>" + b"a" * 1000 + b"</w:t></w:r></w:p>"
    bomb = _docx(b"<w:document>" + para * 4000 + b"</w:document>")          # 4 MB of XML
    assert len(bomb) < 64 * 1024
    text, pages, error = extract("big.docx", bomb)
    assert text is None and error and "CAIRN_PIPES_MAX_EXPANDED_BYTES" in error
    # A document within the limit is read as before.
    text, _, error = extract("ok.docx", _docx(b"<w:document>" + para * 3 + b"</w:document>"))
    assert error is None and text == "\n".join(["a" * 1000] * 3)
