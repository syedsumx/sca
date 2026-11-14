"""Document parsing (L112): tables and charts from PDFs and images as JSON. A generated PDF with a real table (in its
text layer) and a real bar chart (drawn, embedded as an image) is parsed: the table comes from the text layer with no
model; the page is rendered (PDFium, in a child process) and shown to the deployment's multimodal model, Anthropic's API
answered by the stand-in from tests/test_ai_gateway.py scripted to return the chart's data, through Cairn's metered
provider. Then the same through the API (as the person, metered, kept for them only) and through a files pipe on the
real Cairn Store and Cairn Catalog (a ``parsed`` column). Needs pypdf, Pillow and pypdfium2; the pipe test also the
store and catalog binaries."""

import base64
import http.server
import io
import json
import threading
import zlib

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("anthropic")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

from test_ai_gateway import Provider  # noqa: E402
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: E402, F401  (fixtures)

pytest.importorskip("pypdfium2")   # PDF pages rendered by PDFium (the documents extra)

TABLE = [["Region", "Q1", "Q2", "Q3"], ["North", "120", "135", "150"], ["South", "90", "80", "95"],
         ["West", "60", "75", "110"]]
TOTALS = [270, 290, 355]
CHART = {"type": "bar", "title": "Units sold per quarter", "x_label": "Quarter", "y_label": "Units",
         "labels": ["Q1", "Q2", "Q3"], "series": [{"name": "Units", "values": TOTALS}]}
BAR = (31, 119, 180)


def chart_png(size=(800, 500)) -> bytes:
    """A bar chart, drawn: axes, three bars in proportion to TOTALS, their labels and a title."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    w, h = size
    d.line([(80, 40), (80, h - 60), (w - 40, h - 60)], fill="black", width=3)
    top = max(TOTALS)
    for i, (label, v) in enumerate(zip(CHART["labels"], TOTALS)):
        x = 140 + i * 200
        d.rectangle([x, h - 60 - int((h - 120) * v / top), x + 120, h - 60], fill=BAR)
        d.text((x + 50, h - 45), label, fill="black")
        d.text((x + 45, h - 80 - int((h - 120) * v / top)), str(v), fill="black")
    d.text((w // 2 - 80, 10), CHART["title"], fill="black")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def make_pdf() -> bytes:
    """Two pages. Page 1: a title, TABLE written cell by cell in the text layer, and the chart (an embedded image).
    Page 2: a paragraph only (nothing drawn: not shown to the model)."""
    chart = Image.open(io.BytesIO(chart_png()))
    raw = zlib.compress(chart.convert("RGB").tobytes())

    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    cells = []
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            cells.append(f"BT /F1 11 Tf {72 + c * 90} {700 - r * 16} Td ({esc(cell)}) Tj ET")
    page1 = "\n".join(["BT /F1 16 Tf 72 740 Td (Quarterly sales by region) Tj ET", *cells,
                       "q 400 0 0 250 72 380 cm /Im1 Do Q",
                       "BT /F1 10 Tf 72 360 Td (Figure 1: units sold per quarter, all regions.) Tj ET"]).encode()
    page2 = "\n".join(f"BT /F1 11 Tf 72 {740 - i * 16} Td ({esc(t)}) Tj ET" for i, t in enumerate([
        "The outlook for next year is steady, with growth expected in every region.",
        "West grew fastest in the third quarter after the new store opened."])).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> "
        b"/XObject << /Im1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
        b"/Filter /FlateDecode /Length %d >>\nstream\n" % (chart.width, chart.height, len(raw)) + raw + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 8 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(page1) + page1 + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(page2) + page2 + b"\nendstream",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


class Scripted(Provider):
    """The model: answers with the chart's data when shown a picture with the chart's bars in it."""
    shown: list = []

    def reply(self, body) -> str:
        content = body["messages"][0]["content"]
        images = [b for b in content if isinstance(b, dict) and b.get("type") == "image"] if isinstance(content, list) else []
        text = next((b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"), "") \
            if isinstance(content, list) else content
        pictures = [Image.open(io.BytesIO(base64.b64decode(b["source"]["data"]))) for b in images]
        Scripted.shown.append({"text": text, "media": [b["source"]["media_type"] for b in images],
                               "sizes": [p.size for p in pictures],
                               "bars": [{c: n for n, c in p.convert("RGB").getcolors(1 << 24)}.get(BAR, 0) for p in pictures]})
        if not any(Scripted.shown[-1]["bars"]):
            return json.dumps({"charts": [], "tables": []})
        return json.dumps({"charts": [CHART],
                           # One the text layer already gave (dropped) and one only the picture has (kept).
                           "tables": [{"title": None, "header": TABLE[0], "rows": TABLE[1:]},
                                      {"title": "Totals", "header": ["Quarter", "Units"],
                                       "rows": [[q, str(v)] for q, v in zip(CHART["labels"], TOTALS)]}]})


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    import anthropic
    from sqlalchemy import create_engine

    from cairn.ai.llm import AnthropicProvider
    from cairn.ai.usage import UsageMeter
    from cairn.common.db import init_db

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('parse')}/cairn.db")
    init_db(engine)
    client = anthropic.Anthropic(base_url=f"http://127.0.0.1:{up.server_address[1]}", api_key="sk-ant-upstream")
    provider = AnthropicProvider("parse-model", "low", client=client)
    yield {"provider": provider, "meter": UsageMeter(engine), "engine": engine,
           "url": f"http://127.0.0.1:{up.server_address[1]}"}
    up.shutdown()


def test_tables_come_from_the_text_layer_without_a_model():
    from cairn.pipelines.parse import parse

    out = parse("report.pdf", make_pdf(), llm=None)
    assert out["page_count"] == 2 and out["tables"] == 1 and out["charts"] == 0 and out["model"] is None
    t = out["pages"][0]["tables"][0]
    assert t == {"source": "text_layer", "header": TABLE[0], "rows": TABLE[1:]}
    assert "Quarterly sales by region" in out["pages"][0]["text"] and out["pages"][1]["tables"] == []
    # The page that draws a chart was not read for charts, and the result says why; page 2 draws nothing.
    assert out["errors"] == ["page(s) 1 draw graphics not read for charts: charts need Cairn's model (CAIRN_AI_*), "
                             "and none is set up"]
    # Deterministic: the same document, the same tables.
    assert parse("report.pdf", make_pdf())["pages"][0]["tables"] == out["pages"][0]["tables"]


def test_charts_are_read_by_the_multimodal_model(model):
    from sqlalchemy import select

    from cairn.ai.usage import MeteredProvider, set_actor, usage_table
    from cairn.pipelines.parse import parse

    Scripted.shown.clear()
    set_actor("urn:li:corpuser:ana")
    out = parse("report.pdf", make_pdf(), MeteredProvider(model["provider"], model["meter"]))
    p1 = out["pages"][0]
    assert p1["charts"] == [{"source": "model", **CHART, "series": [{"name": "Units", "values": [270.0, 290.0, 355.0]}]}]
    # The text layer's table, then the one only the picture has; the model's copy of the first is dropped.
    assert [t["source"] for t in p1["tables"]] == ["text_layer", "model"]
    assert p1["tables"][1]["header"] == ["Quarter", "Units"] and p1["tables"][1]["rows"][2] == ["Q3", "355"]
    assert out["pages"][1]["charts"] == [] and out["model"] == "parse-model" and out["model_pages"] == 1
    assert out["errors"] == []
    # The model was shown page 1 only, rendered whole as PNG (the chart's bars in it), told the known table.
    assert len(Scripted.shown) == 1 and Scripted.shown[0]["media"] == ["image/png"]
    assert Scripted.shown[0]["bars"][0] > 1000 and Scripted.shown[0]["sizes"][0][0] > 600
    assert "Page 1" in Scripted.shown[0]["text"] and "Region | Q1 | Q2 | Q3" in Scripted.shown[0]["text"]
    # Metered as the person.
    with model["engine"].connect() as c:
        used = c.execute(select(usage_table.c.actor, usage_table.c.task)).all()
    assert ("urn:li:corpuser:ana", "document_parse") in {tuple(u) for u in used}


def test_charts_in_image_files_and_refusals(model):
    from cairn.ai.usage import MeteredProvider
    from cairn.pipelines.parse import ParseError, parse

    Scripted.shown.clear()
    out = parse("chart.png", chart_png(), MeteredProvider(model["provider"], model["meter"]))
    assert out["page_count"] == 1 and out["charts"] == 1 and out["pages"][0]["charts"][0]["labels"] == ["Q1", "Q2", "Q3"]
    assert len(Scripted.shown) == 1 and Scripted.shown[0]["bars"][0] > 1000
    with pytest.raises(ParseError, match="PDFs and images"):
        parse("notes.txt", b"hello")
    with pytest.raises(ParseError, match="not a readable PDF"):
        parse("broken.pdf", b"%PDF-1.4 nothing here")
    # The model's page budget: pages beyond it are reported, not shown.
    import os

    os.environ["CAIRN_PARSE_MAX_MODEL_PAGES"] = "1"
    try:
        many = io.BytesIO()
        frames = [Image.open(io.BytesIO(chart_png())).convert("RGB") for _ in range(3)]
        frames[0].save(many, format="TIFF", save_all=True, append_images=frames[1:])
        out = parse("charts.tiff", many.getvalue(), MeteredProvider(model["provider"], model["meter"]))
        assert out["charts"] == 1 and out["model_pages"] == 1 and "CAIRN_PARSE_MAX_MODEL_PAGES" in out["errors"][0]
    finally:
        os.environ.pop("CAIRN_PARSE_MAX_MODEL_PAGES")


def test_the_api_parses_as_the_person_and_keeps_results_for_them(model, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from cairn.ai.usage import usage_table
    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    settings = Settings(database_url=f"sqlite:///{tmp_path}/cairn.db", service_token="svc-secret",
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")})
    s = build_services(settings, graph=InMemoryGraph(), llm=model["provider"], with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ai", effect="allow", actions=["USE_AI_FUNCTIONS"],
               condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"})]))
    api = TestClient(create_api_app(s))
    ana, bob = {"X-Cairn-Actor": "urn:li:corpuser:ana"}, {"X-Cairn-Actor": "urn:li:corpuser:bob"}
    doc = {"name": "report.pdf", "content": base64.b64encode(make_pdf()).decode()}
    Scripted.shown.clear()
    r = api.post("/v1/documents/parse", json=doc, headers=ana)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["charts"] == 1 and got["tables"] == 2 and got["pages"][0]["charts"][0]["title"] == CHART["title"]
    assert got["pages"][0]["text"].startswith("Quarterly sales") and len(Scripted.shown) == 1
    # Bob may not send pages to a model (USE_AI_FUNCTIONS) ...
    assert api.post("/v1/documents/parse", json=doc, headers=bob).status_code == 403
    # ... but may have the text layer's tables, with no model.
    plain = api.post("/v1/documents/parse", json={**doc, "charts": False}, headers=bob).json()
    assert plain["tables"] == 1 and plain["charts"] == 0 and len(Scripted.shown) == 1
    # Kept for the person who asked: listed, read back (tables and charts, not the text), and nobody else's.
    mine = api.get("/v1/documents/parsed", headers=ana).json()
    assert [(d["name"], d["tables"], d["charts"], d["page_count"]) for d in mine] == [("report.pdf", 2, 1, 2)]
    full = api.get(f"/v1/documents/parsed/{got['id']}", headers=ana).json()
    assert full["pages"][0]["charts"] == got["pages"][0]["charts"] and "text" not in full["pages"][0]
    assert api.get(f"/v1/documents/parsed/{got['id']}", headers=bob).status_code == 404
    assert [d["id"] for d in api.get("/v1/documents/parsed", headers=bob).json()] == [plain["id"]]
    # Bad input.
    assert api.post("/v1/documents/parse", json={"name": "x.pdf", "content": "%%%"}, headers=ana).status_code == 400
    # Metered as ana; audited with counts only.
    with s.engine.connect() as c:
        used = c.execute(select(usage_table.c.actor, usage_table.c.task)).all()
    assert {tuple(u) for u in used} == {("urn:li:corpuser:ana", "document_parse")}
    events = [e for e in s.audit.entries("ai", 500) if e["action"] == "document.parsed"]
    assert len(events) == 2 and events[-1]["actor"] == "urn:li:corpuser:ana" and events[-1]["details"]["charts"] == 1
    assert all("Quarterly" not in json.dumps(e["details"]) for e in events)
    assert api.delete(f"/v1/documents/parsed/{got['id']}", headers=ana).status_code == 204
    assert api.get("/v1/documents/parsed", headers=ana).json() == []


@pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
def test_files_pipes_parse_documents_into_a_column(lake, model, tmp_path):  # noqa: F811
    import boto3
    from sqlalchemy import create_engine

    from cairn.ai.usage import MeteredProvider
    from cairn.common.db import init_db
    from cairn.pipelines.pipes import Pipes

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    p = Pipes(engine, store={"endpoint": lake["store"], "access_key": STORE_KEY[0], "secret_key": STORE_KEY[1],
                             "region": "us-east-1"},
              catalog={"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                       "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                       "s3.path-style-access": "true"})
    p.llm = MeteredProvider(model["provider"], model["meter"])
    s3 = boto3.client("s3", endpoint_url=lake["store"], aws_access_key_id=STORE_KEY[0],
                      aws_secret_access_key=STORE_KEY[1], region_name="us-east-1")
    s3.put_object(Bucket="lakehouse", Key="parse-in/report.pdf", Body=make_pdf())
    s3.put_object(Bucket="lakehouse", Key="parse-in/chart.png", Body=chart_png())
    s3.put_object(Bucket="lakehouse", Key="parse-in/notes.txt", Body=b"no tables here")
    pipe = p.create({"name": "reports", "bucket": "lakehouse", "prefix": "parse-in/", "format": "files",
                     "target": "raw.reports", "create_table": True, "ai": {"parse": True}}, "urn:li:corpuser:finance-bot")
    assert pipe["ai"] == {"parse": True}
    assert {f["status"] for f in p.sweep(pipe["id"])} == {"ok"}
    rows = {r["key"]: r for r in p._catalog().load_table(("raw", "reports")).scan().to_arrow().to_pylist()}
    report = json.loads(rows["parse-in/report.pdf"]["parsed"])
    assert report["pages"][0]["tables"][0]["rows"] == TABLE[1:] and report["pages"][0]["charts"][0]["labels"] == CHART["labels"]
    assert "text" not in report["pages"][0] and rows["parse-in/report.pdf"]["text"].startswith("Quarterly")
    assert json.loads(rows["parse-in/chart.png"]["parsed"])["charts"] == 1
    assert rows["parse-in/notes.txt"]["parsed"] is None and rows["parse-in/notes.txt"]["parse_error"] is None
    assert "description" not in rows["parse-in/report.pdf"]        # no enrichment columns asked for


def test_an_oversized_page_is_refused_before_it_is_drawn(tmp_path, monkeypatch):
    """PDFium renders in a child process, and a page whose pixels would pass OCR's limit is refused before any
    are drawn (a hostile PDF can declare a huge page)."""
    from pypdf import PdfWriter

    from cairn.pipelines import parse

    w = PdfWriter()
    w.add_blank_page(width=14400, height=14400)   # 200 x 200 inches: 22,000 x 22,000 pixels at 110 dpi
    path = tmp_path / "huge.pdf"
    with open(path, "wb") as f:
        w.write(f)
    with pytest.raises(parse.ParseError, match="over the OCR limit"):
        parse.render_page(str(path), 1)
    monkeypatch.setenv("CAIRN_PARSE_DPI", "1")   # the same page at 1 dpi is small enough
    png = parse.render_page(str(path), 1)
    assert png.startswith(b"\x89PNG")
