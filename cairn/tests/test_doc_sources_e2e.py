"""Document sources (Openflow's SharePoint and Google Drive connectors, Cairn's way): folders kept in a Cairn Store
prefix and indexed by a files pipe, with the real store, catalog and pipe, and a local stand-in for Microsoft Graph
and the Google Drive API (their sign-in, listings with pages and subfolders, downloads and exports; the service
account's signed token is checked). Covered:
- SharePoint: an app's client-credentials sign-in, files in subfolders and on a second page, written to the prefix
  and indexed with their text; a changed file rewritten, a moved one written at its new path, a deleted one removed;
- Google Drive: a service account's signed token, a Google Doc exported as Word, a PDF, a subfolder; a form skipped;
- files over the size limit skipped and listed; a run is idempotent;
- keys come from a secret scope as the owner: a wrong secret or an unreadable scope fails the run, told and audited."""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from test_pipes_e2e import LANDING_KEY, _docx, _pdf, world  # noqa: F401  (fixture)
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")
OWNER = "urn:li:corpuser:ops"


class Remote:
    """What SharePoint and Google Drive hold, and what the stand-in served."""

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.secret = "sp-secret-" + "z" * 20
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.sa_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
        self.sa_public = key.public_key()
        # SharePoint: drive "d1", folder Contracts; id -> (parent, name, bytes or None for a folder, etag)
        self.sp = {"root-c": (None, "Contracts", None, ""), "f-msa": ("root-c", "msa.pdf", _pdf("Master services agreement"), "1"),
                   "d-sub": ("root-c", "sub", None, ""), "f-notes": ("d-sub", "notes.txt", b"Renewal in March.", "1"),
                   "f-memo": ("root-c", "memo.docx", _docx("Board memo", "Approve the budget"), "1"),
                   "f-big": ("root-c", "scan.tiff", b"x" * 5000, "1")}
        # Google Drive: folder "gf1"; id -> (parent, name, mime, bytes, md5)
        self.gd = {"g-doc": ("gf1", "Pricing", "application/vnd.google-apps.document", _docx("Pricing", "Gold is 20"), ""),
                   "g-pdf": ("gf1", "terms.pdf", "application/pdf", _pdf("Terms of service"), "m1"),
                   "g-sub": ("gf1", "notes", "application/vnd.google-apps.folder", None, ""),
                   "g-txt": ("g-sub", "call.txt", "text/plain", b"Customer asked for a refund.", "m2"),
                   "g-form": ("gf1", "Survey", "application/vnd.google-apps.form", None, "")}
        self.tokens = set()
        self.calls = []


def handler(remote: Remote):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            return self.headers.get("Authorization", "").removeprefix("Bearer ") in remote.tokens

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            path = urlsplit(self.path).path
            remote.calls.append(("POST", path))
            if path == "/ms/tenant-1/oauth2/v2.0/token":
                if form.get("client_secret") != [remote.secret] or form.get("grant_type") != ["client_credentials"]:
                    return self._send(401, {"error": "invalid_client"})
                tok = "sp-" + str(len(remote.tokens))
            elif path == "/google/token":
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import padding

                head, claims, sig = form["assertion"][0].split(".")
                pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
                try:
                    remote.sa_public.verify(pad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
                except Exception:  # noqa: BLE001 - a forged assertion
                    return self._send(401, {"error": "invalid_grant"})
                if json.loads(pad(claims))["scope"] != "https://www.googleapis.com/auth/drive.readonly":
                    return self._send(403, {"error": "scope"})
                tok = "g-" + str(len(remote.tokens))
            else:
                return self._send(404, {})
            remote.tokens.add(tok)
            self._send(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 3600})

        def do_GET(self):
            u = urlsplit(self.path)
            path, q = unquote(u.path), parse_qs(u.query)
            remote.calls.append(("GET", path))
            if not self._authed():
                return self._send(401, {"error": "unauthenticated"})
            base = f"http://127.0.0.1:{self.server.server_address[1]}/graph/v1.0"
            if path.startswith("/graph/v1.0/drives/d1/"):
                rest = path.removeprefix("/graph/v1.0/drives/d1/")
                if rest.endswith("/content"):
                    item = rest.split("/")[1]
                    return self._send(200, remote.sp[item][2], "application/octet-stream")
                if rest == "root:/Contracts:/children":
                    parent = "root-c"
                elif rest.startswith("items/") and rest.endswith("/children"):
                    parent = rest.split("/")[1]
                else:
                    return self._send(404, {})
                kids = [{"id": i, "name": n, **({"folder": {}} if b is None else
                                                {"file": {}, "size": len(b), "eTag": e})}
                        for i, (p, n, b, e) in sorted(remote.sp.items()) if p == parent]
                page = int((q.get("page") or ["0"])[0])
                out = {"value": kids[page * 2:page * 2 + 2]}           # two per page
                if len(kids) > (page + 1) * 2:
                    out["@odata.nextLink"] = f"{base}/drives/d1/{rest}?page={page + 1}"
                return self._send(200, out)
            if path == "/drive/v3/files":
                parent = q["q"][0].split("'")[1]
                return self._send(200, {"files": [
                    {"id": i, "name": n, "mimeType": m, **({"size": str(len(b))} if b and not m.startswith(
                        "application/vnd.google-apps") else {}), **({"md5Checksum": h} if h else {}),
                     "modifiedTime": "2026-11-01T10:00:00Z"} for i, (p, n, m, b, h) in remote.gd.items() if p == parent]})
            if path.startswith("/drive/v3/files/"):
                fid = path.split("/")[4]
                p, n, m, b, h = remote.gd[fid]
                if path.endswith("/export"):
                    assert q["mimeType"][0].endswith("wordprocessingml.document")
                    return self._send(200, b, q["mimeType"][0])
                assert q.get("alt") == ["media"]
                return self._send(200, b, m)
            self._send(404, {})

        def log_message(self, *a):
            pass
    return H


@pytest.fixture
def sources(world, monkeypatch, tmp_path):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.pipelines.doc_sources import DocumentSources

    monkeypatch.setenv("CAIRN_SAAS_ALLOW_HTTP", "1")
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.setenv("CAIRN_DOC_SOURCES_MAX_BYTES", "4000")
    remote = Remote()
    p = port()
    server = ThreadingHTTPServer(("127.0.0.1", p), handler(remote))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine = create_engine(f"sqlite:///{tmp_path}/docs.db")
    init_db(engine)
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    scopes.create(OWNER, {"name": "sp-keys"})
    for k, v in {"tenant_id": "tenant-1", "client_id": "cairn-app", "client_secret": remote.secret,
                 "login_url": f"http://127.0.0.1:{p}/ms", "graph_url": f"http://127.0.0.1:{p}/graph/v1.0"}.items():
        scopes.put(OWNER, "sp-keys", k, v)
    scopes.create(OWNER, {"name": "gd-keys"})
    scopes.put(OWNER, "gd-keys", "service_account_json", json.dumps({
        "type": "service_account", "client_email": "cairn-docs@example.iam.gserviceaccount.com",
        "private_key": remote.sa_pem, "token_uri": f"http://127.0.0.1:{p}/google/token"}))
    scopes.put(OWNER, "gd-keys", "base_url", f"http://127.0.0.1:{p}")
    audited = []

    class Audit:
        def record(self, *a):
            audited.append(a)
    store = {"endpoint": world["pipes"].store["endpoint"], "access_key": LANDING_KEY[0], "secret_key": LANDING_KEY[1],
             "region": "us-east-1"}
    d = DocumentSources(engine, store, scopes.read, Audit())
    yield {"d": d, "remote": remote, "scopes": scopes, "audited": audited, **world}
    server.shutdown()


def keys(s3, prefix):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket="landing", Prefix=prefix).get("Contents") or [])


def table(p, target):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        return {r["key"]: r for r in p._catalog().load_table(tuple(target.split("."))).scan().to_arrow().to_pylist()}
    except NoSuchTableError:
        return {}


def test_a_sharepoint_folder_is_kept_in_the_store_and_indexed(sources):
    d, s3, p, remote = sources["d"], sources["s3"], sources["pipes"], sources["remote"]
    d.save(OWNER, {"name": "contracts", "kind": "sharepoint", "location": {"drive_id": "d1", "folder": "Contracts"},
                   "bucket": "landing", "prefix": "sp/", "secret_scope": "sp-keys", "cron": "0 * * * *"})
    out = d.run("contracts")
    assert out["status"] == "ok" and out["written"] == 3, out
    assert out["skipped"] == [{"path": "scan.tiff", "size": 5000}]               # over the size limit
    assert keys(s3, "sp/") == ["sp/memo.docx", "sp/msa.pdf", "sp/sub/notes.txt"]
    assert any("page=1" in c[1] or c[1].endswith("/children") for c in remote.calls)
    # A files pipe indexes them: their text is in the table.
    pipe = p.create({"name": "sp-docs", "bucket": "landing", "prefix": "sp/", "format": "files", "target": "raw.sp_docs",
                     "create_table": True}, "urn:li:corpuser:admin")
    p.sweep(pipe["id"])
    t = table(p, "raw.sp_docs")
    assert "Master services agreement" in t["sp/msa.pdf"]["text"] and t["sp/sub/notes.txt"]["text"] == "Renewal in March."
    assert t["sp/memo.docx"]["text"] == "Board memo\nApprove the budget"
    # Again, nothing changed: nothing written.
    assert d.run("contracts")["unchanged"] == 3
    # A changed file, a moved one and a deleted one.
    remote.sp["f-notes"] = ("d-sub", "notes.txt", b"Renewal moved to April.", "2")
    remote.sp["f-msa"] = ("d-sub", "msa.pdf", remote.sp["f-msa"][2], "1")
    del remote.sp["f-memo"]
    out = d.run("contracts")
    assert (out["written"], out["deleted"]) == (2, 1), out
    assert keys(s3, "sp/") == ["sp/sub/msa.pdf", "sp/sub/notes.txt"]
    p.sweep(pipe["id"])
    t = table(p, "raw.sp_docs")
    assert set(t) == {"sp/sub/msa.pdf", "sp/sub/notes.txt"} and t["sp/sub/notes.txt"]["text"] == "Renewal moved to April."
    assert d.get("contracts")["files"] == 2 and d.get("contracts")["status"] == "ok"
    assert [a[1] for a in sources["audited"]].count("document_source.synced") == 3


def test_a_google_drive_folder_with_google_docs(sources):
    d, s3 = sources["d"], sources["s3"]
    d.save(OWNER, {"name": "sales-drive", "kind": "google_drive", "location": {"folder_id": "gf1"},
                   "bucket": "landing", "prefix": "gd/", "secret_scope": "gd-keys"})
    out = d.run("sales-drive")
    assert out["status"] == "ok" and out["written"] == 3, out
    assert keys(s3, "gd/") == ["gd/Pricing.docx", "gd/notes/call.txt", "gd/terms.pdf"]       # the form is skipped
    body = s3.get_object(Bucket="landing", Key="gd/Pricing.docx")["Body"].read()
    assert body == sources["remote"].gd["g-doc"][3]                                           # exported as Word
    assert d.run("sales-drive")["unchanged"] == 3


def test_keys_are_read_as_the_owner_and_failures_are_recorded(sources):
    d, scopes = sources["d"], sources["scopes"]
    scopes.put(OWNER, "sp-keys", "client_secret", "wrong")
    d.save(OWNER, {"name": "bad-secret", "kind": "sharepoint", "location": {"drive_id": "d1", "folder": "Contracts"},
                   "bucket": "landing", "prefix": "bad/", "secret_scope": "sp-keys"})
    out = d.run("bad-secret")
    assert out["status"] == "failed" and "sign-in refused (401)" in out["error"] and "wrong" not in out["error"]
    assert d.get("bad-secret")["status"] == "failed"
    d.save("urn:li:corpuser:mallory", {"name": "not-mine", "kind": "google_drive", "location": {"folder_id": "gf1"},
                                       "bucket": "landing", "prefix": "x/", "secret_scope": "gd-keys"})
    out = d.run("not-mine")
    assert out["status"] == "failed" and "cannot read service_account_json" in out["error"]
    assert ("data", "document_source.failed") == sources["audited"][-1][:2]
    from cairn.pipelines.doc_sources import DocumentSourceError

    for bad in ({"kind": "dropbox"}, {"location": {"folder": "x"}}, {"prefix": "no-slash"}, {"prefix": "../up/"},
                {"cron": "every day"}):
        with pytest.raises(DocumentSourceError):
            d.save(OWNER, {"name": f"x{time.time_ns()}", "kind": "sharepoint", "location": {"drive_id": "d1"},
                           "bucket": "landing", "prefix": "p/", "secret_scope": "sp-keys", **bad})
