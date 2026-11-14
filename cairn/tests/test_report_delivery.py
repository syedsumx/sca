"""L55: a scheduled export reaches Slack as a file in each recipient's own direct messages, their own copy only;
a shared Slack channel (a route) is told but never given a file, and a link opens the recipient's own copy. A Cairn
server renders each copy as its recipient from a real SQLite warehouse; Slack's Web API is a local stand-in that
records what it is sent (chat.postMessage, files.getUploadURLExternal, the upload, files.completeUploadExternal)."""

import http.server
import json
import socket
import tempfile
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, text

from cairn.governance.abac import Policy, PolicyBundle

ANA, BOB, CAROL = (f"urn:li:corpuser:{n}" for n in ("ana", "bob", "carol"))
SLACK_IDS = {ANA: "UANA", BOB: "UBOB", CAROL: "UCAROL"}


class SlackStub:
    def __init__(self):
        stub = self
        self.messages: list[dict] = []
        self.uploads: dict[str, bytes] = {}
        self.shared: list[dict] = []

        class H(http.server.BaseHTTPRequestHandler):
            def _json(self, body):
                out = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                assert self.headers["Authorization"] == "Bearer xoxb-test"
                if u.path.endswith("/files.getUploadURLExternal"):
                    fid = f"F{len(stub.uploads) + len(stub.shared) + 1}"
                    stub.uploads[fid] = b""
                    return self._json({"ok": True, "file_id": fid, "upload_url": f"http://127.0.0.1:{stub.port}/upload/{fid}",
                                       "filename": q["filename"], "length": int(q["length"])})
                self._json({"ok": False, "error": "unknown_method"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.path.startswith("/upload/"):
                    stub.uploads[self.path.rsplit("/", 1)[1]] = body
                    self.send_response(200)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                d = json.loads(body or b"{}")
                if self.path.endswith("/chat.postMessage"):
                    stub.messages.append(d)
                    return self._json({"ok": True, "channel": "D" + d["channel"][1:], "ts": "1.0"})
                if self.path.endswith("/files.completeUploadExternal"):
                    stub.shared.append(d)
                    return self._json({"ok": True, "files": d["files"]})
                self._json({"ok": False, "error": "unknown_method"})

            def log_message(self, *a):
                pass

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stack(monkeypatch):
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.notify.hub import Route
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    slack = SlackStub()
    monkeypatch.setenv("CAIRN_SLACK_API_URL", f"http://127.0.0.1:{slack.port}/api")
    d = tempfile.mkdtemp(prefix="deliver-")
    with create_engine(f"sqlite:///{d}/wh.db").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 10), (2, 'US', 20)"))
    graph = InMemoryGraph()
    graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                active={"slack_bot_token": "xoxb-test", "public_url": "https://cairn.example.com"},
                                gateway={"key_dir": f"{d}/keys"}),
                       graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": f"sqlite:///{d}/wh.db", "catalog": {"platform": "sqlite"}}})
    for who, sid in SLACK_IDS.items():
        st = s.notify.get_settings(who)
        st.channels["slack"].user_id = sid
        s.notify.put_settings(who, st)
    s.notify.put_routes([Route(id="sales-channel", channel="slack", target="CSALES", event_types=["report.ready"])])
    return s, slack


def test_each_recipient_gets_their_own_copy_as_a_file_in_their_own_slack_dm(stack):
    s, slack = stack
    q = s.sql_workspace.save(ANA, {"name": "Orders", "connection": "wh", "sql": "SELECT id, region, amount FROM main.orders ORDER BY id"})
    board = s.sql_boards.save_dashboard(ANA, {"name": "Sales", "tiles": [{"query": q["id"]}]})
    s.bi_sharing.set(ANA, "dashboard", board["id"], BOB, "VIEWER")          # Carol may not open it
    rep = s.report_schedules.save(ANA, {"name": "Weekly sales", "target": {"dashboard": board["id"]}, "format": "csv",
                                        "every_minutes": 1440, "recipients": [BOB, CAROL]})
    out = s.report_schedules.run(rep["id"])
    assert {x["recipient"] for x in out["sent"]} == {ANA, BOB} and [x["recipient"] for x in out["skipped"]] == [CAROL]
    s.notify.dispatch_pending()

    files = {x["recipient"]: s.report_schedules.file(x["recipient"], x["file"]) for x in out["sent"]}
    by_channel = {sh["channel_id"]: slack.uploads[sh["files"][0]["id"]] for sh in slack.shared}
    # Each copy went to its own recipient's DM only, byte for byte; nothing to Carol, nothing to the shared channel.
    assert by_channel == {"DANA": files[ANA]["data"], "DBOB": files[BOB]["data"]}
    assert b"EU" in files[BOB]["data"]
    told = {m["channel"] for m in slack.messages}
    assert told == {"UANA", "UBOB", "CSALES"}                                 # the channel is told, without a file
    # The link opens the recipient's own copy (through the API, signed in).
    links = [n["action_url"] for n in s.notify.inbox(BOB) if n["type"] == "report.ready"]
    assert links and links[0].startswith("https://cairn.example.com/cairn/api/sql/reports/files/")
    # A notification for one person never carries another's file.
    ana_file = next(x["file"] for x in out["sent"] if x["recipient"] == ANA)
    assert s.report_schedules.attachment(ana_file, BOB) is None
    assert s.report_schedules.attachment(ana_file, ANA)[2] == files[ANA]["data"]
