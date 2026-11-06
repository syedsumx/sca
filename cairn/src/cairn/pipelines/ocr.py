"""OCR for scanned documents (queue row 23): Tesseract reads images, and the PDF pages that have no text layer.

Used by ``documents.extract`` (files pipes), so a folder of scans becomes searchable text like any other document.
Tesseract runs as a separate process per page, with a time limit, on an image whose size is checked before it is
decoded; a page that cannot be read is reported on the file's row and never stops the pipe.

Settings (environment):

* ``CAIRN_OCR``: ``auto`` (default: on when the Tesseract binary is there), ``on`` (a file that needs OCR
  without the binary gets an error on its row) or ``off``;
* ``CAIRN_OCR_COMMAND``: the binary (``tesseract``; Cairn's image has it);
* ``CAIRN_OCR_LANGUAGES``: Tesseract languages, ``eng`` or several as ``eng+deu`` (each needs its language data);
* ``CAIRN_OCR_MAX_PAGES`` (100): pages read per document, the rest reported as not read;
* ``CAIRN_OCR_TIMEOUT`` (60): seconds per page, ``CAIRN_OCR_DOCUMENT_TIMEOUT`` (600) per document (the pages
  after it are reported as not read);
* ``CAIRN_OCR_MAX_PIXELS`` (60,000,000): the largest image read (a 600 dpi A3 scan is about 35 million).
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from typing import Iterator, Optional

IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
# A page with fewer characters than this in its text layer is treated as scanned (stray page numbers, crop marks).
MIN_TEXT = 16


class OcrError(Exception):
    pass


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def mode() -> str:
    m = os.environ.get("CAIRN_OCR", "auto").strip().lower()
    return m if m in ("auto", "on", "off") else "auto"


def command() -> Optional[str]:
    """The Tesseract binary when OCR is on and it is there; None otherwise."""
    if mode() == "off":
        return None
    return shutil.which(os.environ.get("CAIRN_OCR_COMMAND", "tesseract"))


def unavailable() -> Optional[str]:
    """Why a document that needs OCR gets none: None when OCR runs, or when it is not asked for (auto or off)."""
    if mode() == "on" and command() is None:
        return f"OCR needs {os.environ.get('CAIRN_OCR_COMMAND', 'tesseract')} (CAIRN_OCR=on), which is not installed"
    return None


def languages() -> str:
    langs = os.environ.get("CAIRN_OCR_LANGUAGES", "eng").strip()
    if not re.fullmatch(r"[A-Za-z_]{3,32}(\+[A-Za-z_]{3,32}){0,9}", langs):
        raise OcrError(f"CAIRN_OCR_LANGUAGES: Tesseract language codes joined by '+' (got {langs[:40]!r})")
    return langs


def max_pages() -> int:
    return _int("CAIRN_OCR_MAX_PAGES", 100)


def document_budget() -> int:
    return _int("CAIRN_OCR_DOCUMENT_TIMEOUT", 600)


def _pixels_ok(width: int, height: int) -> None:
    cap = _int("CAIRN_OCR_MAX_PIXELS", 60_000_000)
    if width <= 0 or height <= 0 or width * height > cap:
        raise OcrError(f"an image of {width}x{height} pixels is over the OCR limit ({cap:,} pixels)")


def _png(img) -> bytes:
    """An image (PIL) as the PNG Tesseract reads: greyscale or RGB, its size already checked."""
    _pixels_ok(*img.size)
    if img.mode not in ("L", "RGB", "1"):
        img = img.convert("RGBA").convert("RGB") if "A" in img.mode or img.mode == "P" else img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def recognise(png: bytes) -> str:
    """The text Tesseract reads in one image."""
    cmd = command()
    if cmd is None:
        raise OcrError("OCR is not available")
    try:
        r = subprocess.run([cmd, "stdin", "stdout", "-l", languages(), "--psm", "3"], input=png, capture_output=True,
                           timeout=_int("CAIRN_OCR_TIMEOUT", 60),
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1",
                                **({"TESSDATA_PREFIX": os.environ["TESSDATA_PREFIX"]} if "TESSDATA_PREFIX" in os.environ else {})})
    except subprocess.TimeoutExpired as e:
        raise OcrError(f"OCR took over {_int('CAIRN_OCR_TIMEOUT', 60)} s") from e
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OcrError(f"Tesseract: {(msg[-1] if msg else f'exit {r.returncode}')[:300]}")
    return r.stdout.decode("utf-8", "replace").strip()


def image_frames(data: bytes) -> Iterator[bytes]:
    """Each frame of an image file (a multi-page TIFF has several) as PNG, sizes checked before decoding."""
    from PIL import Image, ImageSequence

    with Image.open(io.BytesIO(data)) as img:          # reads the header only
        for n, frame in enumerate(ImageSequence.Iterator(img)):
            if n >= max_pages():
                return
            _pixels_ok(*frame.size)
            yield _png(frame.copy())


def frame_count(data: bytes) -> int:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return int(getattr(img, "n_frames", 1))


def page_images(page) -> Iterator[bytes]:
    """The images a PDF page draws (a scan is one per page), as PNG; each one's declared size is checked before
    its stream is decoded."""
    try:
        xobjects = page["/Resources"]["/XObject"].get_object()
    except (KeyError, TypeError, AttributeError):
        return
    for name in list(xobjects.keys()):
        x = xobjects[name].get_object()
        if x.get("/Subtype") != "/Image":
            continue
        _pixels_ok(int(x.get("/Width", 0)), int(x.get("/Height", 0)))
        yield _png(page.images[name].image)
