"""Scheduled exports: a dashboard or workbook sent as CSV or PDF on a schedule, each recipient their own view.

``POST /sql/reports`` with ``{"name", "target": {"dashboard": id} | {"workbook": id}, "format": "csv" | "pdf",
"every_minutes": 1440, "recipients": [people]}`` (the owner is always a recipient). At each run, every recipient's
copy is rendered **as that recipient**: their row filters and masks apply, a recipient who may not open the
dashboard or workbook gets nothing (and the run says so), and a tile they may not see shows its refusal, not its
data. Each copy is kept for 7 days and offered only to its recipient (``GET /sql/reports/files/<id>``); the
recipient is told (event ``report.ready``, by their usual channels): email carries the file as an attachment, and
Slack uploads it into the recipient's own direct messages; neither ever carries another person's copy.

Runs are claimed so that one replica sends each (``every_minutes``: 60 to 43,200), and ``POST …/<id>/run`` sends one
now (the owner). Audited: ``report.saved``, ``report.sent``, ``report.deleted``.
"""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, LargeBinary, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.workspace import WorkspaceError

KEEP_MS = 7 * 24 * 3600 * 1000
MAX_RECIPIENTS = 50
PDF_ROWS = 60

report_schedules = Table(
    "cairn_report_schedules", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("target_kind", String(16), nullable=False),          # dashboard | workbook
    Column("target_id", String(32), nullable=False),
    Column("format", String(8), nullable=False),                # csv | pdf
    Column("every_minutes", Integer, nullable=False),
    Column("recipients", Text, nullable=False),
    Column("next_ms", BigInteger, nullable=False, index=True),
    Column("last_ms", BigInteger, nullable=True),
    Column("last_status", String(500), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
)
report_files = Table(
    "cairn_report_files", metadata,
    Column("id", String(32), primary_key=True),
    Column("schedule", String(32), nullable=False, index=True),
    Column("recipient", String(256), nullable=False, index=True),
    Column("filename", String(256), nullable=False),
    Column("content_type", String(64), nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("expires_ms", BigInteger, nullable=False, index=True),
)


def _now() -> int:
    return int(time.time() * 1000)


# ---- files ------------------------------------------------------------------------------------
def to_csv(sections: list[dict]) -> bytes:
    """One CSV: per section a ``# title`` line, its header and rows (or its error), and a blank line."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for sec in sections:
        w.writerow([f"# {sec['title']}"])
        if sec.get("error"):
            w.writerow([f"(not shown: {sec['error']})"])
        else:
            w.writerow(sec["columns"])
            for r in sec["rows"]:
                w.writerow(["" if v is None else v for v in r])
        w.writerow([])
    return buf.getvalue().encode("utf-8")


def _pdf_text(s: str) -> str:
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def to_pdf(title: str, subtitle: str, sections: list[dict]) -> bytes:
    """A plain PDF (no dependencies): the title, then each section as a monospaced table (up to ``PDF_ROWS`` rows)."""
    lines: list[tuple[str, int, str]] = [("F2", 16, title), ("F1", 9, subtitle), ("F1", 9, "")]
    for sec in sections:
        lines.append(("F2", 12, sec["title"]))
        if sec.get("error"):
            lines.append(("F1", 9, f"Not shown: {sec['error']}"))
        else:
            cols, rows = sec["columns"], sec["rows"][:PDF_ROWS]
            cells = [[str(c) for c in cols]] + [["" if v is None else str(v) for v in r] for r in rows]
            widths = [min(24, max(len(r[i]) for r in cells)) for i in range(len(cols))]
            fmt = lambda r, widths=widths: "  ".join(v[:w].ljust(w) for v, w in zip(r, widths))  # noqa: E731
            lines.append(("F3", 8, fmt(cells[0])))
            lines.append(("F3", 8, "  ".join("-" * w for w in widths)))
            lines += [("F3", 8, fmt(r)) for r in cells[1:]]
            if len(sec["rows"]) > PDF_ROWS:
                lines.append(("F1", 8, f"... {len(sec['rows']) - PDF_ROWS} more rows in the CSV export"))
        lines.append(("F1", 9, ""))
    pages, page, y = [], [], 800
    for font, size, text in lines:
        if y < 50:
            pages.append(page)
            page, y = [], 800
        page.append(f"BT /{font} {size} Tf 40 {y} Td ({_pdf_text(text)}) Tj ET")
        y -= size + 5
    pages.append(page)
    objs: list[bytes] = []
    fonts = "<< /F1 3 0 R /F2 4 0 R /F3 5 0 R >>"
    kids = " ".join(f"{6 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for base in ("Helvetica", "Helvetica-Bold", "Courier"):
        objs.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode())
    for i, p in enumerate(pages):
        content = "\n".join(p).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font {fonts} >> "
                    f"/Contents {7 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


# ---- schedules ---------------------------------------------------------------------------------
class Reports:
    def __init__(self, engine, boards, workbooks, notify: Optional[Callable[..., Any]] = None, audit=None):
        """``notify(type, title, body, recipients, payload)`` tells a recipient their copy is ready."""
        self.engine, self.boards, self.wb, self.notify, self.audit = engine, boards, workbooks, notify, audit

    def _target(self, actor: str, kind: str, tid: str) -> dict:
        if kind == "dashboard":
            return self.boards.dashboard(actor, tid)
        if kind == "workbook":
            from cairn.sqlworkspace.workbooks import WorkbookError

            try:
                return self.wb.get(actor, tid)
            except WorkbookError as e:
                raise WorkspaceError(str(e), e.status)
        raise WorkspaceError("target: {dashboard: id} or {workbook: id}")

    def save(self, actor: str, body: dict, sid: Optional[str] = None) -> dict:
        if not isinstance(body, dict):
            raise WorkspaceError("a report is an object")
        target = body.get("target") or {}
        kind = "dashboard" if target.get("dashboard") else "workbook" if target.get("workbook") else ""
        tid = str(target.get(kind) or "")
        self._target(actor, kind, tid)                            # one the owner may open
        name = str(body.get("name") or "").strip()
        fmt = body.get("format", "csv")
        every = body.get("every_minutes", 1440)
        recipients = body.get("recipients") or []
        if not name:
            raise WorkspaceError("name: required")
        if fmt not in ("csv", "pdf"):
            raise WorkspaceError("format: csv or pdf")
        if isinstance(every, bool) or not isinstance(every, int) or not 60 <= every <= 43_200:
            raise WorkspaceError("every_minutes: 60 to 43,200")
        if not isinstance(recipients, list) or len(recipients) > MAX_RECIPIENTS or \
                not all(isinstance(r, str) and r.startswith("urn:li:corpuser:") for r in recipients):
            raise WorkspaceError(f"recipients: up to {MAX_RECIPIENTS} people (urn:li:corpuser:…)")
        people = [actor] + [r for r in dict.fromkeys(recipients) if r != actor]
        values = {"name": name[:200], "target_kind": kind, "target_id": tid, "format": fmt, "every_minutes": every,
                  "recipients": json.dumps(people), "enabled": bool(body.get("enabled", True))}
        with self.engine.begin() as conn:
            if sid is None:
                sid = uuid.uuid4().hex
                conn.execute(insert(report_schedules).values(id=sid, owner=actor, next_ms=_now() + every * 60_000, **values))
            elif not conn.execute(update(report_schedules).where(and_(report_schedules.c.id == sid,
                                                                      report_schedules.c.owner == actor))
                                  .values(**values)).rowcount:
                raise WorkspaceError("no such report of yours", 404)
        self._audit(actor, "report.saved", sid, {"target": f"{kind}:{tid}", "format": fmt, "recipients": len(people)})
        return self.get(actor, sid)

    @staticmethod
    def _public(r) -> dict:
        d = dict(r._mapping)
        return {**d, "recipients": json.loads(d["recipients"]), "enabled": bool(d["enabled"])}

    def get(self, actor: str, sid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(report_schedules).where(report_schedules.c.id == sid)).first()
        if r is None or r.owner != actor:
            raise WorkspaceError("no such report of yours", 404)
        return self._public(r)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(report_schedules).where(report_schedules.c.owner == actor)
                                .order_by(report_schedules.c.name)).all()
        return [self._public(r) for r in rows]

    def delete(self, actor: str, sid: str) -> None:
        self.get(actor, sid)
        with self.engine.begin() as conn:
            conn.execute(delete(report_schedules).where(report_schedules.c.id == sid))
            conn.execute(delete(report_files).where(report_files.c.schedule == sid))
        self._audit(actor, "report.deleted", sid, {})

    # ---- rendering, as each recipient ---------------------------------------------------------
    def _sections(self, recipient: str, kind: str, tid: str) -> tuple[str, list[dict]]:
        target = self._target(recipient, kind, tid)               # they may open it, or nothing is made
        if kind == "dashboard":
            board = self.boards.render(recipient, tid, {})
            return board["name"], [{"title": t.get("title") or "Tile", "error": t.get("error"),
                                    "columns": (t.get("result") or {}).get("columns") or [],
                                    "rows": (t.get("result") or {}).get("rows") or []} for t in board["tiles"]]
        from cairn.sqlworkspace.workbooks import WorkbookError

        out = []
        for i, el in enumerate(target["spec"]["elements"]):
            title = (el.get("source") or {}).get("table") or f"Element {i + 1}"
            try:
                res = self.wb.run(recipient, target["connection"], el)
                out.append({"title": title, "columns": res["columns"], "rows": res["rows"]})
            except (WorkbookError, WorkspaceError) as e:
                out.append({"title": title, "error": str(e), "columns": [], "rows": []})
        return target["name"], out

    def run(self, sid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(report_schedules).where(report_schedules.c.id == sid)).first()
        if r is None:
            raise WorkspaceError("no such report", 404)
        sched = self._public(r)
        sent, skipped, now = [], [], _now()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for person in sched["recipients"]:
            try:
                title, sections = self._sections(person, sched["target_kind"], sched["target_id"])
            except WorkspaceError as e:
                skipped.append({"recipient": person, "reason": str(e)})
                continue
            user = person.removeprefix("urn:li:corpuser:")
            if sched["format"] == "pdf":
                data, ctype = to_pdf(title, f"{sched['name']} · for {user} · {stamp}", sections), "application/pdf"
            else:
                data, ctype = to_csv(sections), "text/csv"
            fname = f"{sched['name']}-{datetime.now(timezone.utc):%Y%m%d}.{sched['format']}".replace("/", "-")
            fid = uuid.uuid4().hex
            with self.engine.begin() as conn:
                conn.execute(delete(report_files).where(report_files.c.expires_ms < now))
                conn.execute(insert(report_files).values(id=fid, schedule=sid, recipient=person, filename=fname,
                                                         content_type=ctype, data=data, created_ms=now,
                                                         expires_ms=now + KEEP_MS))
            sent.append({"recipient": person, "file": fid})
            if self.notify is not None:
                try:
                    self.notify("report.ready", f"{sched['name']} ({sched['format'].upper()})",
                                f"Your copy of {title}, as you see it, is ready.", [person],
                                {"report": sid, "file": fid, "filename": fname, "url": f"/sql/reports/files/{fid}"})
                except Exception:  # noqa: BLE001 - the file is kept either way
                    pass
        status = f"sent {len(sent)}" + (f", skipped {len(skipped)}" if skipped else "")
        with self.engine.begin() as conn:
            conn.execute(update(report_schedules).where(report_schedules.c.id == sid)
                         .values(last_ms=now, last_status=status[:500]))
        self._audit(sched["owner"], "report.sent", sid, {"sent": len(sent), "skipped": [s["recipient"] for s in skipped]})
        return {"report": sid, "sent": sent, "skipped": skipped}

    def run_due(self, now: Optional[int] = None) -> int:
        now = now or _now()
        with self.engine.connect() as conn:
            due = conn.execute(select(report_schedules.c.id, report_schedules.c.next_ms, report_schedules.c.every_minutes)
                               .where(and_(report_schedules.c.enabled, report_schedules.c.next_ms <= now))).all()
        n = 0
        for sid, next_ms, every in due:
            with self.engine.begin() as conn:                     # claimed: one replica sends each run
                won = conn.execute(update(report_schedules).where(and_(report_schedules.c.id == sid,
                                                                       report_schedules.c.next_ms == next_ms))
                                   .values(next_ms=now + every * 60_000)).rowcount
            if won:
                self.run(sid)
                n += 1
        return n

    def file(self, actor: str, fid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(report_files).where(report_files.c.id == fid)).first()
        if r is None or r.recipient != actor or r.expires_ms < _now():
            raise WorkspaceError("no such report file of yours", 404)
        return {"filename": r.filename, "content_type": r.content_type, "data": r.data}

    def attachment(self, fid: str, recipient: Optional[str] = None) -> Optional[tuple[str, str, bytes]]:
        """For email and Slack: (filename, content type, bytes) of a report file, only to the person it was made
        for (a notification sent elsewhere never carries someone else's copy)."""
        with self.engine.connect() as conn:
            r = conn.execute(select(report_files).where(report_files.c.id == fid)).first()
        if r is None or r.expires_ms < _now() or (recipient is not None and r.recipient != recipient):
            return None
        return (r.filename, r.content_type, r.data)

    def _audit(self, actor: str, action: str, sid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, f"report:{sid}", details)


def build_reports_router(rep: Reports, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/reports", tags=["sql"])

    def person(p) -> str:
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "reports are scheduled by and for people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: rep.list(actor))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: rep.save(actor, body))

    @router.get("/files/{fid}")
    async def download(fid: str, p=Depends(current_principal)):
        actor = person(p)
        f = await call(lambda: rep.file(actor, fid))
        return Response(f["data"], media_type=f["content_type"],
                        headers={"Content-Disposition": f'attachment; filename="{f["filename"]}"'})

    @router.put("/{sid}")
    async def change(sid: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: rep.save(actor, body, sid))

    @router.post("/{sid}/run")
    async def run_now(sid: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: rep.get(actor, sid))
        return await call(lambda: rep.run(sid))

    @router.delete("/{sid}", status_code=204)
    async def remove(sid: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: rep.delete(actor, sid))

    return router
