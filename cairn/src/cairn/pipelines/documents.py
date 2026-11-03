"""Text from documents (files pipes): what a file says, for search, AI and SQL over files.

``extract(name, data)`` returns ``(text, pages, error)``:

* plain text, Markdown, CSV/TSV, JSON, XML, logs: decoded as UTF-8 (invalid bytes replaced);
* HTML: the visible text (scripts, styles and tags dropped);
* PDF: each page's text (``pypdf``, the ``documents`` extra); scanned pages without a text layer give
  nothing (no OCR);
* Word (``.docx``) and PowerPoint (``.pptx``): the text of their paragraphs and slides.

Anything else has no text (``None``). Text is capped at ``CAIRN_PIPES_MAX_TEXT`` characters (1,000,000).
A document that cannot be read gives the reason in ``error``; it never stops the pipe.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from html.parser import HTMLParser
from typing import Optional

TEXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".log", ".yaml", ".yml", ".sql", ".rst"}
HTML = {".html", ".htm"}


def _cap(text: str) -> str:
    return text[: int(os.environ.get("CAIRN_PIPES_MAX_TEXT", "1000000"))]


class _Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _office(data: bytes, members: re.Pattern) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted((n for n in z.namelist() if members.match(n)),
                           key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)]):
            xml = z.read(name).decode("utf-8", "replace")
            # Paragraphs end lines; text runs are joined.
            for para in re.split(r"</(?:w|a):p>", xml):
                runs = re.findall(r"<(?:w|a):t(?:\s[^>]*)?>([^<]*)</(?:w|a):t>", para)
                if runs:
                    out.append("".join(runs))
    import html as _html

    return _html.unescape("\n".join(out))


def extract(name: str, data: bytes) -> tuple[Optional[str], Optional[int], Optional[str]]:
    ext = os.path.splitext(name.lower())[1]
    try:
        if ext in TEXT:
            return _cap(data.decode("utf-8", "replace")), None, None
        if ext in HTML:
            p = _Visible()
            p.feed(data.decode("utf-8", "replace"))
            text = re.sub(r"\n\s*\n+", "\n\n", "".join(p.parts)).strip()
            return _cap(text), None, None
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return None, None, "PDF text needs pypdf (the documents extra)"
            reader = PdfReader(io.BytesIO(data))
            pages = [(pg.extract_text() or "") for pg in reader.pages]
            return _cap("\n\n".join(pages).strip()), len(pages), None
        if ext == ".docx":
            return _cap(_office(data, re.compile(r"^word/(document|header\d*|footer\d*)\.xml$"))), None, None
        if ext == ".pptx":
            slides = re.compile(r"^ppt/slides/slide\d+\.xml$")
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                n = sum(1 for m in z.namelist() if slides.match(m))
            return _cap(_office(data, slides)), n, None
    except Exception as e:  # noqa: BLE001 - recorded on the row
        return None, None, f"{type(e).__name__}: {str(e)[:300]}"
    return None, None, None
