"""Scheduled exports' files: the CSV and the PDF a recipient gets (the schedules themselves run on real Trino, as
each recipient, in tests/test_trino_e2e.py)."""

import io

from cairn.sqlworkspace.reports import PDF_ROWS, to_csv, to_pdf

SECTIONS = [{"title": "Revenue by region", "columns": ["region", "revenue"], "rows": [["EU", 130.5], ["US", None]]},
            {"title": "Salaries", "error": "you may not query hr.salaries", "columns": [], "rows": []}]


def test_csv_has_a_section_per_tile_and_says_what_was_not_shown():
    text = to_csv(SECTIONS).decode()
    assert text.splitlines() == ["# Revenue by region", "region,revenue", "EU,130.5", "US,", "",
                                 "# Salaries", "(not shown: you may not query hr.salaries)", ""]


def test_pdf_is_a_valid_document_with_the_tables():
    from pypdf import PdfReader

    many = [{"title": "Orders", "columns": ["id", "note (with parens)"], "rows": [[i, f"row {i}"] for i in range(PDF_ROWS + 5)]}]
    data = to_pdf("Sales — weekly", "for ana · 2026-11-04", SECTIONS + many)
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) >= 2                                  # long tables flow onto more pages
    assert "Revenue by region" in text and "EU" in text and "130.5" in text
    assert "Not shown: you may not query hr.salaries" in text
    assert "note (with parens)" in text and "5 more rows in the CSV export" in text
