"""Scheduled exports' files: the CSV and the PDF a recipient gets (the schedules themselves run on real Trino, as
each recipient, in tests/test_trino_e2e.py)."""

import io

import pytest

from cairn.sqlworkspace.reports import PDF_ROWS, to_csv, to_pdf

SECTIONS = [{"title": "Revenue by region", "columns": ["region", "revenue"], "rows": [["EU", 130.5], ["US", None]]},
            {"title": "Salaries", "error": "you may not query hr.salaries", "columns": [], "rows": []}]


def test_csv_has_a_section_per_tile_and_says_what_was_not_shown():
    text = to_csv(SECTIONS).decode()
    assert text.splitlines() == ["# Revenue by region", "region,revenue", "EU,130.5", "US,", "",
                                 "# Salaries", "(not shown: you may not query hr.salaries)", ""]


def _pdf():
    many = [{"title": "Orders", "columns": ["id", "note (with parens)"], "rows": [[i, f"row {i}"] for i in range(PDF_ROWS + 5)]}]
    return to_pdf("Sales — weekly", "for ana · 2026-11-04", SECTIONS + many)


def test_pdf_is_well_formed_with_the_tables_in_it():
    import re

    data = _pdf()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    # Every entry of the cross-reference table points at its object, and startxref at the table.
    xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref:xref + 4] == b"xref"
    count = int(re.search(rb"xref\n0 (\d+)", data).group(1))
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n ", data)]
    assert len(offsets) == count - 1
    for n, off in enumerate(offsets, 1):
        assert data[off:off + len(f"{n} 0 obj")] == f"{n} 0 obj".encode()
    assert data.count(b"/Type /Page ") >= 2                       # long tables flow onto more pages
    for text in (b"Revenue by region", b"130.5", b"Not shown: you may not query hr.salaries", b"note \\(with parens\\)",
                 b"5 more rows in the CSV export"):
        assert text in data, text


def test_pdf_reads_back_in_a_pdf_library():
    pypdf = pytest.importorskip("pypdf")                           # the documents extra (the trino CI job has it)
    reader = pypdf.PdfReader(io.BytesIO(_pdf()))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) >= 2 and "Revenue by region" in text and "EU" in text
    assert "note (with parens)" in text and "Not shown: you may not query hr.salaries" in text


def test_a_file_downloads_whatever_the_report_is_called():
    """The report's name becomes the file's name: quotes, line breaks and characters outside Latin-1 must neither
    break the download (a header Starlette cannot encode is a 500) nor reshape the Content-Disposition header."""
    from types import SimpleNamespace
    from urllib.parse import unquote

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.sqlworkspace.reports import Reports, build_reports_router, report_files, report_schedules

    class Boards:
        def dashboard(self, actor, tid):
            return {"id": tid, "name": "Sales"}

        def render(self, actor, tid, params):
            return {"name": "Sales", "tiles": [{"title": "T", "result": {"columns": ["a"], "rows": [[1]]}}]}

    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(engine, tables=[report_schedules, report_files])
    rep = Reports(engine, Boards(), None)
    me = "urn:li:corpuser:ana"
    sid = rep.save(me, {"name": 'Q3 "sales" → \U0001F4C8\r\nX-Evil: 1', "target": {"dashboard": "d1"}})["id"]
    fid = rep.run(sid)["sent"][0]["file"]
    app = FastAPI()
    app.include_router(build_reports_router(rep, lambda: SimpleNamespace(urn=me, is_service=False, via="web")))
    r = TestClient(app).get(f"/sql/reports/files/{fid}")
    assert r.status_code == 200 and r.content.startswith(b"# T")
    assert "x-evil" not in r.headers
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="Q3 -sales- ') and cd.count('"') == 2, cd
    encoded = cd.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded).startswith('Q3 "sales" → \U0001F4C8  X-Evil: 1-') and unquote(encoded).endswith(".csv")
