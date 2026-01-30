"""PDF export API route."""

import io
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# Shared in-memory store reference (same as analysis.py)
from codescope.api.routes.analysis import _analyses


def _build_pdf(analysis_data: dict) -> bytes:
    """Generate a PDF report from analysis results."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
        PageBreak,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"], fontSize=24, spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        "Subtitle2",
        parent=styles["Normal"],
        fontSize=12,
        textColor=colors.grey,
        spaceAfter=20,
    )
    h1 = ParagraphStyle(
        "H1", parent=styles["Heading1"], fontSize=16, spaceAfter=10, spaceBefore=16
    )
    h2 = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontSize=13, spaceAfter=8, spaceBefore=12
    )
    body = styles["Normal"]
    small = ParagraphStyle("Small", parent=body, fontSize=8, textColor=colors.grey)

    elements: list = []

    result = analysis_data.get("result")
    project_name = analysis_data.get("project_name", "Unknown")
    timestamp = analysis_data.get("timestamp", datetime.now())

    # ── Title page ──────────────────────────────────────────────
    elements.append(Spacer(1, 60))
    elements.append(Paragraph("CodeScope SCA Report", title_style))
    elements.append(
        Paragraph(
            f"Project: {project_name} &bull; "
            f"Generated: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            subtitle_style,
        )
    )
    elements.append(
        HRFlowable(width="100%", thickness=2, color=colors.HexColor("#3b82f6"))
    )
    elements.append(Spacer(1, 20))

    # Collect issues from the result object
    issues_list: list = []
    if result:
        for issue in result.all_issues:
            issues_list.append(
                {
                    "type": issue.type.value if hasattr(issue.type, "value") else str(issue.type),
                    "severity": issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity),
                    "rule_id": issue.rule_id,
                    "message": issue.message,
                    "file": str(issue.location.file_path) if issue.location else "",
                    "line": issue.location.start_line if issue.location else 0,
                }
            )

    total = len(issues_list)
    type_counts = Counter(i["type"] for i in issues_list)
    severity_counts = Counter(i["severity"] for i in issues_list)

    vulns = [i for i in issues_list if i["type"] == "VULNERABILITY"]
    bugs = [i for i in issues_list if i["type"] == "BUG"]
    smells = [i for i in issues_list if i["type"] == "CODE_SMELL"]

    # ── Executive Summary ───────────────────────────────────────
    elements.append(Paragraph("Executive Summary", h1))

    summary_data = [
        ["Metric", "Count"],
        ["Total Issues", str(total)],
        ["Vulnerabilities", str(type_counts.get("VULNERABILITY", 0))],
        ["Bugs", str(type_counts.get("BUG", 0))],
        ["Code Smells", str(type_counts.get("CODE_SMELL", 0))],
    ]
    t = Table(summary_data, colWidths=[200, 100])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("PADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(t)
    elements.append(Spacer(1, 15))

    # ── Severity breakdown ──────────────────────────────────────
    elements.append(Paragraph("Severity Breakdown", h2))
    sev_data = [["Severity", "Count"]]
    for sev in ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]:
        c = severity_counts.get(sev, 0)
        if c > 0:
            sev_data.append([sev, str(c)])
    if len(sev_data) > 1:
        t2 = Table(sev_data, colWidths=[200, 100])
        t2.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("ALIGN", (1, 0), (1, -1), "CENTER"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("PADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        elements.append(t2)
    elements.append(Spacer(1, 10))

    # ── Vulnerabilities ─────────────────────────────────────────
    elements.append(PageBreak())
    elements.append(Paragraph("Vulnerabilities", h1))

    if vulns:
        vuln_data = [["#", "Rule", "File", "Line", "Message"]]
        for idx, v in enumerate(vulns, 1):
            vuln_data.append(
                [
                    str(idx),
                    v["rule_id"],
                    Paragraph(v["file"], small),
                    str(v["line"]),
                    Paragraph(v["message"][:100], small),
                ]
            )
        t3 = Table(vuln_data, colWidths=[25, 85, 160, 35, 210])
        t3.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dc2626")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("ALIGN", (3, 0), (3, -1), "CENTER"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fef2f2")]),
                    ("PADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        elements.append(t3)
    else:
        elements.append(Paragraph("No vulnerabilities detected.", body))

    # ── Bugs ────────────────────────────────────────────────────
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("Bugs", h1))
    elements.append(Paragraph(f"<b>{len(bugs)}</b> bugs detected.", body))
    elements.append(Spacer(1, 8))

    if bugs:
        bug_rules = Counter(b["rule_id"] for b in bugs)
        bug_table = [["Rule ID", "Count", "Sample Message"]]
        for rule, count in bug_rules.most_common(15):
            sample = next(b["message"] for b in bugs if b["rule_id"] == rule)
            bug_table.append([rule, str(count), Paragraph(sample[:80], small)])
        t4 = Table(bug_table, colWidths=[150, 50, 315])
        t4.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ea580c")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 0), (1, -1), "CENTER"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff7ed")]),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        elements.append(t4)

    # ── Code Smells ─────────────────────────────────────────────
    elements.append(PageBreak())
    elements.append(Paragraph("Code Smells", h1))
    elements.append(Paragraph(f"<b>{len(smells)}</b> code smells detected.", body))
    elements.append(Spacer(1, 8))

    if smells:
        smell_rules = Counter(s["rule_id"] for s in smells)
        smell_table = [["Rule ID", "Count", "Sample Message"]]
        for rule, count in smell_rules.most_common(15):
            sample = next(s["message"] for s in smells if s["rule_id"] == rule)
            smell_table.append([Paragraph(rule, small), str(count), Paragraph(sample[:80], small)])
        t5 = Table(smell_table, colWidths=[150, 50, 315])
        t5.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d97706")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 0), (1, -1), "CENTER"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fffbeb")]),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        elements.append(t5)

    # ── Top files by issues ─────────────────────────────────────
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("Top Files by Issues", h1))

    file_counts = Counter(i["file"] for i in issues_list if i["file"])
    if file_counts:
        file_table = [["#", "File", "Issues"]]
        for idx, (filepath, count) in enumerate(file_counts.most_common(20), 1):
            file_table.append([str(idx), Paragraph(filepath, small), str(count)])
        t6 = Table(file_table, colWidths=[25, 410, 50])
        t6.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6366f1")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("ALIGN", (2, 0), (2, -1), "CENTER"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2ff")]),
                    ("PADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        elements.append(t6)

    # ── Footer ──────────────────────────────────────────────────
    elements.append(Spacer(1, 30))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    elements.append(Spacer(1, 10))
    elements.append(
        Paragraph(
            f"Report generated by CodeScope v0.1.0 on {datetime.now().strftime('%B %d, %Y at %H:%M')}",
            ParagraphStyle("Footer", parent=body, fontSize=8, textColor=colors.grey, alignment=1),
        )
    )

    doc.build(elements)
    return buf.getvalue()


@router.get("/export/pdf/{analysis_id}")
async def export_pdf(analysis_id: str):
    """Export analysis results as a PDF report."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]

    if data["status"] != "SUCCESS":
        raise HTTPException(
            status_code=400,
            detail=f"Analysis is not complete (status: {data['status']})",
        )

    try:
        pdf_bytes = _build_pdf(data)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="reportlab is required for PDF export. Install with: pip install reportlab",
        )
    except Exception as exc:
        logger.error("PDF generation failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to generate PDF report")

    project_name = data.get("project_name", "report")
    filename = f"codescope-{project_name}-{analysis_id}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
