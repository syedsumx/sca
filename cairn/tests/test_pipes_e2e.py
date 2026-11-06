"""Pipes (cairn.pipelines.pipes) with real Cairn Stores and Cairn Catalog: files landing in one store are
loaded into Iceberg tables in the lakehouse, as soon as the store's signed event arrives, and by a sweep
for anything an event missed; each file once, failures recorded per file. Needs the store and catalog
binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port, wait  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
SECRET = "pipes-webhook-secret-" + "x" * 16
LANDING_KEY = ("AKLANDING0000000", "landing-secret-" + "y" * 24)


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    import uvicorn
    from fastapi import FastAPI, Request
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.pipelines.pipes import Pipes, build_pipes_router

    d = tmp_path_factory.mktemp("pipes")
    engine = create_engine(f"sqlite:///{d}/cairn.db")
    init_db(engine)
    api = port()
    p = Pipes(engine,
              store={"endpoint": "", "access_key": LANDING_KEY[0], "secret_key": LANDING_KEY[1], "region": "us-east-1"},
              catalog={"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                       "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                       "s3.path-style-access": "true"},
              webhook_secret=SECRET)

    def who(request: Request):
        return Principal("urn:li:corpuser:admin", "token")
    app = FastAPI()
    app.include_router(build_pipes_router(p, who, lambda pr, priv, urn: None))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api, log_level=os.environ.get("PIPES_LOG", "warning"),
                                           timeout_keep_alive=75))
    threading.Thread(target=server.run, daemon=True).start()
    wait(api)
    # The landing store, telling Cairn about new objects (signed webhook).
    sp = port()
    targets = d / "targets.json"
    targets.write_text(json.dumps([{"type": "webhook", "id": "cairn-pipes", "url": f"http://127.0.0.1:{api}/v1/pipes/events",
                                    "secret": SECRET}]))
    sdir = tempfile.mkdtemp()
    store = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": LANDING_KEY[0], "CAIRN_STORE_SECRET_KEY": LANDING_KEY[1],
        "CAIRN_STORE_DATA": sdir, "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "landing",
        "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_EVENT_TARGETS_FILE": str(targets)})
    wait(sp)
    p.store["endpoint"] = f"http://127.0.0.1:{sp}"
    import boto3
    s3 = boto3.client("s3", endpoint_url=p.store["endpoint"], aws_access_key_id=LANDING_KEY[0],
                      aws_secret_access_key=LANDING_KEY[1], region_name="us-east-1")
    s3.put_bucket_notification_configuration(Bucket="landing", NotificationConfiguration={"QueueConfigurations": [{
        "Id": "pipes", "QueueArn": "arn:cairn:store:::cairn-pipes", "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "orders/"}]}}}]})
    yield {"pipes": p, "s3": s3, "api": f"http://127.0.0.1:{api}"}
    server.should_exit = True
    store.terminate()
    store.wait(15)
    shutil.rmtree(sdir, ignore_errors=True)


def rows(p, target):
    return p._catalog().load_table(tuple(target.split("."))).scan().to_arrow().num_rows


def wait_for(fn, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def test_files_are_loaded_when_they_land_and_only_once(world):
    import httpx

    p, s3, api = world["pipes"], world["s3"], world["api"]
    r = httpx.post(f"{api}/v1/pipes", json={"name": "orders", "bucket": "landing", "prefix": "orders/", "format": "csv",
                                            "target": "raw.orders", "create_table": True}, timeout=10)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    s3.put_object(Bucket="landing", Key="orders/2026-11-01.csv", Body=b"id,region,amount\n1,EU,9.5\n2,US,3.0\n")
    assert wait_for(lambda: [f for f in p.files(pid) if f["status"] == "ok"]), p.files(pid)   # by the store's event
    assert rows(p, "raw.orders") == 2
    s3.put_object(Bucket="landing", Key="orders/2026-11-02.csv", Body=b"id,region,amount\n3,EU,7.0\n")
    assert wait_for(lambda: rows(p, "raw.orders") == 3)
    # A file that does not fit is recorded as failed; the next one still loads.
    s3.put_object(Bucket="landing", Key="orders/bad.csv", Body=b"nothing,useful\n1,2\n")
    s3.put_object(Bucket="landing", Key="orders/2026-11-03.csv", Body=b"id,region,amount\n4,US,1.0\n")
    assert wait_for(lambda: rows(p, "raw.orders") == 4)
    bad = wait_for(lambda: [f for f in p.files(pid) if f["key"] == "orders/bad.csv"])
    assert bad[0]["status"] == "error" and bad[0]["error"]
    # Each file once: a sweep finds nothing new, and the table is unchanged.
    assert [f for f in p.sweep(pid) if f["status"] == "ok"] == []
    assert rows(p, "raw.orders") == 4
    # The webhook takes only the store's signature.
    body = json.dumps({"Records": []}).encode()
    assert httpx.post(f"{api}/v1/pipes/events", content=body, headers={"x-cairn-signature": "sha256=00"}).status_code == 401


def test_the_sweep_loads_what_no_event_announced(world):
    p, s3 = world["pipes"], world["s3"]
    pipe = p.create({"name": "late", "bucket": "landing", "prefix": "late/", "format": "json", "target": "raw.late",
                     "create_table": True}, "urn:li:corpuser:admin")
    s3.put_object(Bucket="landing", Key="late/a.json", Body=b'{"k": "a", "n": 1}\n{"k": "b", "n": 2}\n')
    s3.put_object(Bucket="landing", Key="late/_SUCCESS", Body=b"")          # markers are not data
    time.sleep(2)
    assert p.files(pipe["id"]) == []                                         # no rule for late/: no event
    done = p.sweep(pipe["id"])
    assert [(f["key"], f["status"], f["rows"]) for f in done] == [("late/a.json", "ok", 2)]
    assert rows(p, "raw.late") == 2 and p.sweep(pipe["id"]) == []


def _pdf(text: str) -> bytes:
    """A one-page PDF with a text layer (written by hand: offsets and all)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _docx(*paragraphs: str) -> bytes:
    import io
    import zipfile

    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_files_pipes_keep_a_directory_table_with_the_text_of_documents(world):
    import hashlib

    p, s3 = world["pipes"], world["s3"]
    s3.put_bucket_notification_configuration(Bucket="landing", NotificationConfiguration={"QueueConfigurations": [
        {"Id": "pipes", "QueueArn": "arn:cairn:store:::cairn-pipes", "Events": ["s3:ObjectCreated:*"],
         "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "orders/"}]}}},
        {"Id": "docs", "QueueArn": "arn:cairn:store:::cairn-pipes", "Events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
         "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "docs/"}]}}}]})
    p.create({"name": "docs", "bucket": "landing", "prefix": "docs/", "format": "files", "target": "raw.docs",
              "create_table": True}, "urn:li:corpuser:admin")
    files = {"docs/notes.txt": b"Quarterly notes: revenue up.", "docs/page.html": b"<html><head><style>x{}</style>"
             b"<script>var x=1</script></head><body><h1>Refund policy</h1><p>Within 30 days.</p></body></html>",
             "docs/contract.pdf": _pdf("Master services agreement"), "docs/memo.docx": _docx("Board memo", "Approve the budget"),
             "docs/blob.bin": b"\x00\x01\x02"}
    for k, v in files.items():
        s3.put_object(Bucket="landing", Key=k, Body=v)

    def table():
        from pyiceberg.exceptions import NoSuchTableError

        try:
            t = p._catalog().load_table(("raw", "docs")).scan().to_arrow().to_pylist()
        except NoSuchTableError:
            return {}
        return {r["key"]: r for r in t}
    pid = next(x["id"] for x in p.list() if x["name"] == "docs")
    assert wait_for(lambda: len(table()) == 5), (sorted(table()), p.files(pid))
    t = table()
    assert t["docs/notes.txt"]["text"] == "Quarterly notes: revenue up." and t["docs/notes.txt"]["content_type"] == "text/plain"
    assert "Refund policy" in t["docs/page.html"]["text"] and "var x" not in t["docs/page.html"]["text"]
    assert "Master services agreement" in t["docs/contract.pdf"]["text"] and t["docs/contract.pdf"]["pages"] == 1
    assert t["docs/memo.docx"]["text"] == "Board memo\nApprove the budget"
    assert t["docs/blob.bin"]["text"] is None and t["docs/blob.bin"]["size"] == 3
    assert t["docs/notes.txt"]["sha256"] == hashlib.sha256(files["docs/notes.txt"]).hexdigest()
    assert t["docs/contract.pdf"]["path"] == "s3://landing/docs/contract.pdf"
    # A changed object replaces its row; a deleted one goes.
    s3.put_object(Bucket="landing", Key="docs/notes.txt", Body=b"Quarterly notes: revenue up 12%.")
    assert wait_for(lambda: (table().get("docs/notes.txt") or {}).get("text") == "Quarterly notes: revenue up 12%.")
    assert len(table()) == 5
    s3.delete_object(Bucket="landing", Key="docs/blob.bin")
    assert wait_for(lambda: "docs/blob.bin" not in table()) and len(table()) == 4


def test_the_sweep_also_forgets_objects_that_are_gone(world):
    p, s3 = world["pipes"], world["s3"]
    pipe = p.create({"name": "archive", "bucket": "landing", "prefix": "archive/", "format": "files", "target": "raw.archive",
                     "create_table": True}, "urn:li:corpuser:admin")
    s3.put_object(Bucket="landing", Key="archive/a.md", Body=b"# Title\nbody")
    assert [f["status"] for f in p.sweep(pipe["id"])] == ["ok"]
    assert p._catalog().load_table(("raw", "archive")).scan().to_arrow().to_pylist()[0]["text"] == "# Title\nbody"
    s3.delete_object(Bucket="landing", Key="archive/a.md")                   # no event rule for archive/
    assert [(f["key"], f["status"]) for f in p.sweep(pipe["id"])] == [("archive/a.md", "removed")]
    assert p._catalog().load_table(("raw", "archive")).scan().to_arrow().num_rows == 0


def _scan(*lines: str):
    """A page as a scanner gives it: an image of text, at 200 dpi."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1700, 2200), 255)
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=48)
    for i, line in enumerate(lines):
        draw.text((150, 200 + i * 90), line, fill=0, font=font)
    return img


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs Tesseract (apt install tesseract-ocr)")
def test_scanned_documents_are_read_by_ocr(world, monkeypatch):
    import io

    from pypdf import PdfReader, PdfWriter

    p, s3 = world["pipes"], world["s3"]
    pipe = p.create({"name": "scans", "bucket": "landing", "prefix": "scans/", "format": "files", "target": "raw.scans",
                     "create_table": True}, "urn:li:corpuser:admin")
    # A PDF whose first page has a text layer and whose second is a scan; an image file; a page over the size limit.
    scanned = io.BytesIO()
    _scan("Invoice 4471 from Northwind Traders", "Total due: 1,250.00 EUR").save(scanned, format="PDF", resolution=200)
    w = PdfWriter()
    for src in (_pdf("Cover letter for the invoice"), scanned.getvalue()):
        for page in PdfReader(io.BytesIO(src)).pages:
            w.add_page(page)
    mixed = io.BytesIO()
    w.write(mixed)
    note = io.BytesIO()
    _scan("Delivery note 88", "Signed by the courier").save(note, format="PNG")
    s3.put_object(Bucket="landing", Key="scans/invoice.pdf", Body=mixed.getvalue())
    s3.put_object(Bucket="landing", Key="scans/note.png", Body=note.getvalue())
    assert sorted(f["status"] for f in p.sweep(pipe["id"])) == ["ok", "ok"]
    rows = {r["key"]: r for r in p._catalog().load_table(("raw", "scans")).scan().to_arrow().to_pylist()}
    inv = rows["scans/invoice.pdf"]
    assert inv["pages"] == 2 and inv["extract_error"] is None, inv["extract_error"]
    assert inv["text"].startswith("Cover letter for the invoice")                 # the text layer, as it is
    assert "Invoice 4471 from Northwind Traders" in inv["text"] and "1,250.00 EUR" in inv["text"]   # the scan, read
    assert rows["scans/note.png"]["text"] == "Delivery note 88\nSigned by the courier" and rows["scans/note.png"]["pages"] == 1
    # Limits are reported on the row, and the rest of the document is still read.
    monkeypatch.setenv("CAIRN_OCR_MAX_PIXELS", "1000000")
    s3.put_object(Bucket="landing", Key="scans/invoice.pdf", Body=mixed.getvalue() + b"\n")   # changed: read again
    p.sweep(pipe["id"])
    inv = {r["key"]: r for r in p._catalog().load_table(("raw", "scans")).scan().to_arrow().to_pylist()}["scans/invoice.pdf"]
    assert inv["text"] == "Cover letter for the invoice"
    assert "page 2" in inv["extract_error"] and "over the OCR limit" in inv["extract_error"]
    # OCR asked for (CAIRN_OCR=on) and not installed: the row says so.
    monkeypatch.setenv("CAIRN_OCR", "on")
    monkeypatch.setenv("CAIRN_OCR_COMMAND", "no-such-tesseract")
    s3.put_object(Bucket="landing", Key="scans/note.png", Body=note.getvalue() + b"\0")
    p.sweep(pipe["id"])
    note_row = {r["key"]: r for r in p._catalog().load_table(("raw", "scans")).scan().to_arrow().to_pylist()}["scans/note.png"]
    assert note_row["text"] is None and "not installed" in note_row["extract_error"]
