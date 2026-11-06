"""Text from documents (files pipes): what a file says, for search, AI and SQL over files.

``extract(name, data)`` returns ``(text, pages, error)``:

* plain text, Markdown, CSV/TSV, JSON, XML, logs: decoded as UTF-8 (invalid bytes replaced);
* HTML: the visible text (scripts, styles and tags dropped);
* PDF: each page's text (``pypdf``, the ``documents`` extra); a scanned page (no text layer) is read by OCR
  (``ocr.py``: Tesseract, when it is installed);
* images (PNG, JPEG, TIFF, BMP, GIF, WebP): read by OCR, a page per frame;
* Word (``.docx``) and PowerPoint (``.pptx``): the text of their paragraphs and slides.

Anything else has no text (``None``). Text is capped at ``CAIRN_PIPES_MAX_TEXT`` characters (1,000,000).
A document that cannot be read gives the reason in ``error``; it never stops the pipe.
"""

from __future__ import annotations

import io
import os
import re
import time
import zipfile
from html.parser import HTMLParser
from typing import Optional

from cairn.pipelines import ocr

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


def _pdf(reader) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Each page's text layer; a page without one (a scan) is read by OCR from the images it draws."""
    texts, problems, read, skipped = [], [], 0, {"pages": [], "time": []}
    run = ocr.command() is not None
    until = time.monotonic() + ocr.document_budget()
    for n, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if len(text) < ocr.MIN_TEXT:
            if not run:
                if (why := ocr.unavailable()) and why not in problems:
                    problems.append(why)
            elif read >= ocr.max_pages():
                skipped["pages"].append(n)
            elif time.monotonic() > until:
                skipped["time"].append(n)
            else:
                read += 1
                try:
                    scanned = [ocr.recognise(png) for png in ocr.page_images(page)]
                    text = "\n".join(t for t in [text, *scanned] if t).strip()
                except ocr.OcrError as e:
                    problems.append(f"page {n}: {e}")
                except Exception as e:  # noqa: BLE001 - an image pypdf or Pillow cannot decode (JBIG2, broken streams)
                    problems.append(f"page {n}: image not readable ({type(e).__name__}: {str(e)[:200]})")
        texts.append(text)
    for why, pages in (("OCR reads %d pages a document" % ocr.max_pages(), skipped["pages"]),
                       ("OCR has %d s a document" % ocr.document_budget(), skipped["time"])):
        if pages:
            problems.append(f"{len(pages)} scanned page(s) not read, from page {pages[0]} ({why})")
    return _cap("\n\n".join(texts).strip()), len(texts), "; ".join(problems)[:2000] or None


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
            return _pdf(reader)
        if ext in ocr.IMAGES:
            if ocr.command() is None:
                return None, None, ocr.unavailable()
            pages, problems = [], []
            until = time.monotonic() + ocr.document_budget()
            try:
                for png in ocr.image_frames(data):
                    if time.monotonic() > until:
                        problems.append(f"page {len(pages) + 1} on: not read (OCR has {ocr.document_budget()} s a document)")
                        break
                    try:
                        pages.append(ocr.recognise(png))
                    except ocr.OcrError as e:
                        problems.append(f"page {len(pages) + 1}: {e}")
                        pages.append("")
            except ocr.OcrError as e:              # a frame over the size limit: the frames before it are kept
                problems.append(f"page {len(pages) + 1}: {e}")
            total = ocr.frame_count(data)
            if total > len(pages) and not problems:
                problems.append(f"OCR read the first {len(pages)} of {total} pages (CAIRN_OCR_MAX_PAGES)")
            return _cap("\n\n".join(pages).strip()), total, "; ".join(problems) or None
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
