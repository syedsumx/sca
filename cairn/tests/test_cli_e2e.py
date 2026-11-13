"""The `cairn` command line against a real Cairn API (uvicorn), a real Trino asking Cairn before every action, and
Cairn Catalog and Store underneath (the lakehouse of test_trino_e2e). Each command runs as its own process, the way
people run it (``python -m cairn_cli.main``, or the PyInstaller binary in CAIRN_CLI_BIN). Checked:

- `cairn login`: the device flow (RFC 8628) approved by the signed-in person; tokens kept in a keyring backend
  (keyrings.alt), or in the 0600 file without one, never printed; slow_down, pending, denied and expired codes;
- `whoami`; profiles (several, `use`, --profile, CAIRN_PROFILE) and CAIRN_URL/CAIRN_TOKEN winning for CI;
- `query`: every format, streamed over many pages, row filters and masks as the person, --limit, Ctrl-C cancelling
  the query in Trino, and a table the person may not read giving exit code 4 with the privilege named;
- `search`, `show` and `ask` (with a stand-in for the model provider);
- refresh, then sign-out (`logout`) and revocation from the profile page: the old tokens are refused (exit 3);
- `--token-stdin`.

Needs docker and the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import http.server
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

import httpx
import pytest

from test_trino_e2e import ORDERS, SALARIES, cairn, lake, trino  # noqa: F401 - fixtures used by name

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
Q = "SELECT id, email, region FROM sales.orders ORDER BY id"
BIG = ("SELECT o.id, o.region, a.x FROM sales.orders o CROSS JOIN UNNEST(sequence(1, 5000)) AS a(x) "
       "ORDER BY o.id, a.x")
ENDLESS = ("SELECT o.id, a.x, b.y FROM sales.orders o CROSS JOIN UNNEST(sequence(1, 10000)) AS a(x) "
           "CROSS JOIN UNNEST(sequence(1, 10000)) AS b(y)")
pytestmark = pytest.mark.skipif(not os.environ.get("CAIRN_STORE_BIN"), reason="needs the lakehouse of test_trino_e2e")


class ModelProvider(http.server.BaseHTTPRequestHandler):
    """A stand-in for Anthropic's Messages API: answers the text-to-SQL request with fixed SQL."""
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ModelProvider.seen.append(body)
        text = json.dumps({"sql": "SELECT region, count(*) AS n FROM sales.orders GROUP BY region ORDER BY region",
                           "explanation": "Counts orders per region in sales.orders."})
        out = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                          "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                          "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _search_handler(graph):
    """Cairn Core's catalog search, over the in-memory catalog the test's Cairn uses."""
    def handle(query, variables):
        if "searchAcrossLineage" in query:
            return {"searchAcrossLineage": {"total": 0, "searchResults": []}}
        if "searchAcrossEntities" not in query:
            return {}
        text = str((variables.get("input") or {}).get("query") or "*").lower()
        urns = sorted({u for (u, _a) in graph.aspects if u.startswith("urn:li:dataset:")})
        hits = []
        for u in urns:
            name = u.split(",")[1]
            if text in ("*", "") or text in name.lower():
                hits.append({"entity": {"urn": u, "type": "DATASET", "name": name,
                                        "platform": {"name": u.split("urn:li:dataPlatform:")[1].split(",")[0]},
                                        "properties": {"name": name, "description": ""}}})
        return {"searchAcrossEntities": {"total": len(hits), "searchResults": hits}}
    return handle


@pytest.fixture(scope="module")
def stack(trino, cairn, tmp_path_factory):  # noqa: F811 - the fixtures above
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    s.graph.graphql_handler = _search_handler(s.graph)
    s.graph.aspects[(ORDERS, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:ana", "type": "DATAOWNER"}]}
    s.graph.aspects[(ORDERS, "datasetProperties")] = {"name": "orders", "description": "One row per order."}
    s.device_logins.interval_s = 1                           # quick polls for the test (the default is five seconds)
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelProvider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    if getattr(s, "ask", None) is not None:
        anthropic = pytest.importorskip("anthropic")
        from cairn.ai.llm import AnthropicProvider

        s.ask.sql.llm = AnthropicProvider(model="claude-sonnet-5-5", effort="low", server_side_fallback=False,
                                          client=anthropic.Anthropic(base_url=f"http://127.0.0.1:{up.server_address[1]}",
                                                                     api_key="provider-key-held-by-cairn"))
    home = tmp_path_factory.mktemp("cli-home")
    yield url, s, home
    up.shutdown()


def base_env(home, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CAIRN_URL", "CAIRN_TOKEN", "CAIRN_TOKEN_FILE", "CAIRN_PROFILE",
                                                           "CAIRN_CA_FILE", "PYTHON_KEYRING_BACKEND")}
    env.update({"PYTHONPATH": SRC, "CAIRN_CONFIG_DIR": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                "PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring", "NO_COLOR": "1", "HOME": str(home)})
    env.update(extra)
    return env


BINARY = os.environ.get("CAIRN_CLI_BIN")


def command() -> list[str]:
    return [BINARY] if BINARY else [sys.executable, "-m", "cairn_cli.main"]


def cli(home, *args, input=None, timeout=300, **env):
    r = subprocess.run(command() + list(args), env=base_env(home, **env), input=input, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def approve(url, code, user, decision="approve"):
    h = {"X-Cairn-Actor": user}            # the person's browser session (trusted actor header in this test's Cairn)
    seen = httpx.get(f"{url}/oauth/device", params={"code": code}, headers=h, timeout=10)
    assert seen.status_code == 200 and seen.json()["client"] == "Cairn CLI", seen.text
    r = httpx.post(f"{url}/oauth/device", json={"user_code": code, "decision": decision}, headers=h, timeout=10)
    assert r.status_code == 200, r.text
    return seen.json()


def login(url, home, profile, user, *extra, **env):
    p = subprocess.Popen(command() + ["login", "--url", url, "--no-browser", "--profile", profile, *extra],
                         env=base_env(home, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = []
    code = None
    for line in p.stderr:
        lines.append(line)
        m = re.search(r"enter the code ([A-Z]{4}-[A-Z]{4})", line)
        if m:
            code = m.group(1)
            break
    assert code, "".join(lines)
    seen = approve(url, code, user)
    out, err = p.communicate(timeout=120)
    return p.returncode, out, "".join(lines) + err, seen


def _keyring(home):
    """The keyring backend the commands use (keyrings.alt's plaintext file): the test reads it, nobody else."""
    from keyrings.alt.file import PlaintextKeyring

    kr = PlaintextKeyring()
    kr.file_path = str(home / "data" / "python_keyring" / "keyring_pass.cfg")
    return kr


def stored(home, profile) -> dict:
    if BINARY:                     # the binary has the OS keychains' backends only: here, the 0600 file
        return json.loads((home / "config" / "credentials.json").read_text())[profile]
    return json.loads(_keyring(home).get_password("cairn-cli", profile))


def _rewrite(home, profile, value):
    if BINARY:
        path = home / "config" / "credentials.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), profile: value}))
        return
    _keyring(home).set_password("cairn-cli", profile, json.dumps(value))


def test_device_login_whoami_and_profiles(stack):
    url, s, home = stack
    rc, out, err, seen = login(url, home, "dev", ANA)
    assert rc == 0, err
    assert seen["name"].startswith("cairn CLI on ") and seen["name"].endswith(", profile dev)")   # what the person saw
    assert "Signed in to" in err and ANA in err and ("tokens in the file" if BINARY else "tokens in the keyring") in err
    assert "cla_" not in out + err and "clr_" not in out + err          # tokens are never printed
    cred = stored(home, "dev")
    assert cred["access_token"].startswith("cla_") and cred["refresh_token"].startswith("clr_") and cred["url"] == url
    assert BINARY or not (home / "config" / "credentials.json").exists()  # the keyring, not the file
    cfg = (home / "config" / "config.toml").read_text()
    assert f'url = "{url}"' in cfg and "cla_" not in cfg and 'default_profile = "dev"' in cfg

    rc, out, err = cli(home, "whoami", "--output", "json")
    me = json.loads(out)
    assert rc == 0 and me["user"] == ANA and me["profile"] == "dev" and me["via"] == "cli" and me["url"] == url, err
    assert me["access_token_expires"] > time.time() + 3000 and me["sign_in_expires"] > time.time() + 20 * 86400
    rc, out, _ = cli(home, "whoami")
    assert rc == 0 and "ana" in out and "Access token expires" in out

    # A second profile for another person; switching, --profile and CAIRN_PROFILE.
    assert login(url, home, "bob", BOB)[0] == 0
    rc, out, _ = cli(home, "profiles", "list", "--output", "json")
    listed = {p["name"]: p for p in json.loads(out)["profiles"]}
    assert set(listed) == {"dev", "bob"} and listed["dev"]["default"] and listed["bob"]["signed_in"]
    assert cli(home, "profiles", "use", "bob")[0] == 0
    assert json.loads(cli(home, "whoami", "--output", "json")[1])["user"] == BOB
    assert json.loads(cli(home, "whoami", "--output", "json", "--profile", "dev")[1])["user"] == ANA
    assert json.loads(cli(home, "--profile", "dev", "whoami", "--output", "json")[1])["user"] == ANA
    assert json.loads(cli(home, "whoami", "--output", "json", CAIRN_PROFILE="dev")[1])["user"] == ANA
    assert cli(home, "profiles", "use", "dev")[0] == 0
    rc, _, err = cli(home, "whoami", "--profile", "nope")
    assert rc == 3 and "cairn login --profile nope" in err
    # The people's signed-in devices, as the profile page lists them.
    devices = httpx.get(f"{url}/v1/me/devices", headers={"X-Cairn-Actor": ANA}, timeout=10).json()["devices"]
    assert len(devices) == 1 and devices[0]["name"].startswith("cairn CLI on ") and devices[0]["actor"] == ANA


def test_the_device_endpoint_answers_as_rfc_8628_says(stack):
    url, s, _ = stack
    start = httpx.post(f"{url}/oauth/device_authorization", data={"client_id": "cairn-cli"}, timeout=10).json()
    assert re.match(r"^[B-DF-HJ-NP-TV-XZ]{4}-[B-DF-HJ-NP-TV-XZ]{4}$", start["user_code"])
    assert start["verification_uri"].endswith("/cairn/device") and start["interval"] == 1
    poll = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"],
            "client_id": "cairn-cli"}
    time.sleep(1.1)
    first = httpx.post(f"{url}/oauth/token", data=poll, timeout=10)
    assert first.status_code == 400 and first.json()["error"] == "authorization_pending"
    fast = httpx.post(f"{url}/oauth/token", data=poll, timeout=10)
    assert fast.json()["error"] == "slow_down"                           # polled sooner than the interval
    assert httpx.post(f"{url}/oauth/token", data={**poll, "client_id": "other"}, timeout=10).json()["error"] == "invalid_client"
    approve(url, start["user_code"], BOB, "deny")
    assert httpx.post(f"{url}/oauth/token", data=poll, timeout=10).json()["error"] == "access_denied"
    # An approval needs the person's browser session: a CLI token cannot approve another device.
    other = httpx.post(f"{url}/oauth/device_authorization", data={"client_id": "cairn-cli"}, timeout=10).json()
    import sqlalchemy as sa

    from cairn.identity.agents import agent_tokens

    with s.engine.begin() as conn:                                        # the code's ten minutes are over
        conn.execute(sa.update(agent_tokens).where(agent_tokens.c.kind.in_(("cli_dev", "cli_uc")),
                                                   agent_tokens.c.expires_ms > 0,
                                                   agent_tokens.c.actor == "").values(expires_ms=1))
    expired = httpx.post(f"{url}/oauth/token", data={**poll, "device_code": other["device_code"]}, timeout=10)
    assert expired.json()["error"] == "expired_token"
    assert httpx.get(f"{url}/oauth/device", params={"code": other["user_code"]}, headers={"X-Cairn-Actor": ANA},
                     timeout=10).status_code == 404
    meta = httpx.get(f"{url}/.well-known/oauth-authorization-server", timeout=10).json()
    assert meta["device_authorization_endpoint"].endswith("/oauth/device_authorization")
    assert "urn:ietf:params:oauth:grant-type:device_code" in meta["grant_types_supported"]


BUNDLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "dist", "mfe")
HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Cairn</title></head>
<body><div id="root"></div><script src="/cli-ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def test_the_device_page_and_signed_in_devices_in_a_browser(stack):
    """The person approves `cairn login` on the web UI's /device page (headless Chromium, signed in to Cairn), then
    signs the device out from their profile page."""
    pytest.importorskip("playwright.sync_api")
    if not os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")):
        pytest.skip("needs the built UI bundle (npm run build:mfe in ui/)")
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from playwright.sync_api import sync_playwright

    url, s, home = stack
    app = next(c.cell_contents for c in s.chaos_start_api.__closure__ if isinstance(c.cell_contents, FastAPI))
    if not any(getattr(r, "path", "") == "/cli-host" for r in app.routes):     # the UI, served the way the edge does
        app.mount("/cli-ui", StaticFiles(directory=BUNDLE), name="cli-ui")
        app.add_api_route("/cli-host", lambda: HTMLResponse(HOST), methods=["GET"])
    p = subprocess.Popen(command() + ["login", "--url", url, "--no-browser", "--profile", "web"], env=base_env(home),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    code = None
    for line in p.stderr:
        m = re.search(r"enter the code ([A-Z]{4}-[A-Z]{4})", line)
        if m:
            code = m.group(1)
            break
    assert code
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA},
                                       viewport={"width": 1440, "height": 900}).new_page()
            page.goto(f"{url}/cli-host#/device?code={code}", wait_until="networkidle")
            page.get_by_text("asks to sign in to Cairn as you").wait_for(timeout=20_000)
            assert code in page.inner_text("#root")
            page.get_by_role("button", name="Approve").click()
            page.get_by_text("Device signed in").wait_for(timeout=20_000)
            _, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
            assert json.loads(cli(home, "whoami", "--profile", "web", "--output", "json")[1])["user"] == ANA
            page.goto(f"{url}/cli-host#/profile", wait_until="networkidle")
            device = page.get_by_role("list", name="Signed-in devices")
            device.wait_for(timeout=20_000)
            name = next(t for t in device.locator("strong").all_inner_texts() if t.endswith("profile web)"))
            page.get_by_role("button", name=f"Sign out {name}").click()
            page.get_by_role("button", name="Sign out", exact=True).press("Enter")      # the confirmation
            page.get_by_text(f"Signed out {name}").wait_for(timeout=20_000)
            assert name not in page.get_by_role("list", name="Signed-in devices").inner_text()
        finally:
            browser.close()
    rc, _, err = cli(home, "whoami", "--profile", "web")
    assert rc == 3 and "cairn login --profile web" in err


def test_query_as_the_person_in_every_format(stack, tmp_path):
    url, s, home = stack
    rc, out, err = cli(home, "query", Q, "--format", "csv")
    assert rc == 0, err
    lines = out.strip().splitlines()
    assert lines[0] == "id,email,region" and len(lines) == 4
    assert lines[1].split(",")[1] == "j•••@•••.com" and "masked for you: email" in err      # ana's mask
    rc, out, _ = cli(home, "query", Q, "--format", "json")
    rows = json.loads(out)
    assert rc == 0 and [r["id"] for r in rows] == [1, 2, 3] and rows[0]["email"] == "j•••@•••.com"
    rc, out, _ = cli(home, "query", "--format", "jsonl", input=Q)                           # SQL on standard input
    assert rc == 0 and [json.loads(x)["region"] for x in out.splitlines()] == ["EU", "US", "EU"]
    (tmp_path / "q.sql").write_text(Q + ";\n")
    rc, out, _ = cli(home, "query", "-f", str(tmp_path / "q.sql"), "--format", "table")
    assert rc == 0 and out.splitlines()[0].split() == ["id", "email", "region"] and "─" in out.splitlines()[1]
    pq = pytest.importorskip("pyarrow.parquet")
    if not BINARY:                                                         # the binaries leave Parquet out
        rc, _, err = cli(home, "query", Q, "--format", "parquet", "--out", str(tmp_path / "o.parquet"))
        t = pq.read_table(tmp_path / "o.parquet")
        assert rc == 0 and t.num_rows == 3 and t.column("id").to_pylist() == [1, 2, 3], err
        assert str(t.schema.field("id").type) == "int32"
    rc, out, _ = cli(home, "query", Q, "--format", "csv", "--profile", "bob")                # bob's row filter
    assert rc == 0 and [ln.split(",")[2] for ln in out.strip().splitlines()[1:]] == ["EU", "EU"]
    rc, out, _ = cli(home, "query", Q, "--output", "json", "--limit", "2")
    assert rc == 0 and len(json.loads(out)) == 2


def test_large_results_stream_page_by_page(stack, tmp_path):
    url, s, home = stack
    rc, _, err = cli(home, "query", BIG, "--format", "csv", "--out", str(tmp_path / "big.csv"))
    assert rc == 0, err
    lines = (tmp_path / "big.csv").read_text().splitlines()
    assert len(lines) == 1 + 3 * 5000 and "15000 rows" in err
    rc, out, _ = cli(home, "query", BIG, "--format", "csv", "--profile", "bob")
    assert rc == 0 and len(out.strip().splitlines()) == 1 + 2 * 5000                   # EU only, all pages
    # The pages over the API: more than one, each with a cursor only its person can use.
    token = stored(home, "dev")["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    page = httpx.post(f"{url}/v1/statements", json={"sql": BIG}, headers=h, timeout=60).json()
    pages, rows = 0, 0
    while page.get("next"):
        cursor = page["next"]
        page = httpx.post(f"{url}/v1/statements/next", json={"cursor": cursor}, headers=h, timeout=60).json()
        pages += 1
        rows += len(page.get("rows") or [])
    assert rows == 15000 and pages > 1, (pages, rows)
    bob_token = stored(home, "bob")["access_token"]
    started = httpx.post(f"{url}/v1/statements", json={"sql": BIG}, headers=h, timeout=60).json()
    stolen = httpx.post(f"{url}/v1/statements/next", json={"cursor": started["next"]},
                        headers={"Authorization": f"Bearer {bob_token}"}, timeout=60)
    assert stolen.status_code == 403 and "someone else's" in stolen.text
    httpx.post(f"{url}/v1/statements/cancel", json={"cursor": started["next"]}, headers=h, timeout=60)


def test_ctrl_c_cancels_the_query_in_trino(stack, trino):  # noqa: F811
    url, s, home = stack
    p = subprocess.Popen(command() + ["query", ENDLESS, "--format", "jsonl"], env=base_env(home), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    assert p.stdout.readline()                                            # rows are arriving
    p.send_signal(signal.SIGINT)
    _, err = p.communicate(timeout=120)
    assert p.returncode == 130 and "cancelled" in err, err
    qid = next(e["details"]["query_id"] for e in s.audit.entries("data", 200)
               if e["action"] == "data.queried" and e["details"].get("sql", "").startswith("SELECT o.id, a.x, b.y"))
    end = time.time() + 60
    while True:
        got = trino(f"SELECT state, error_code FROM system.runtime.queries WHERE query_id = '{qid}'")
        if got and got[0]["state"] == "FAILED" or time.time() > end:
            break
        time.sleep(1)
    assert got and got[0]["state"] == "FAILED" and got[0]["error_code"] == "USER_CANCELED", got


def test_refusals_say_which_privilege_on_what(stack):
    url, s, home = stack
    rc, out, err = cli(home, "query", "SELECT * FROM hr.salaries")
    assert rc == 4 and out == "", err
    assert "permission denied" in err and "VIEW_DATASET_PROFILE" in err and SALARIES in err
    rc, _, err = cli(home, "show", SALARIES)
    assert rc == 4 and "VIEW_ENTITY_PAGE" in err and SALARIES in err
    rc, _, err = cli(home, "show", "no.such_table")
    assert rc == 5 and "no asset named no.such_table" in err
    rc, _, err = cli(home, "query", "SELECT 1 FROM sales.nope")
    assert rc == 4 and "not in the catalog" in err
    rc, _, err = cli(home, "qurey", "SELECT 1")
    assert rc == 2 and "did you mean 'query'" in err


def test_search_show_and_ask(stack):
    url, s, home = stack
    rc, out, err = cli(home, "search", "orders", "--output", "json")
    found = {r["urn"] for r in json.loads(out)}
    assert rc == 0 and ORDERS in found and SALARIES not in found, err
    assert SALARIES not in {r["urn"] for r in json.loads(cli(home, "search", "salaries", "--output", "json")[1])}
    rc, out, _ = cli(home, "search", "orders")
    assert rc == 0 and "sales.orders" in out and out.splitlines()[0].split() == ["TYPE", "NAME", "PLATFORM", "URN"]
    rc, out, err = cli(home, "show", "sales.orders")
    assert rc == 0, err
    assert "orders" in out and "One row per order." in out and "Owners:   ana" in out
    assert "email" in out and "Classification.PII.Email" in out and "built from: 0" in out
    doc = json.loads(cli(home, "show", ORDERS, "--output", "json")[1])
    assert [c["name"] for c in doc["columns"]] == ["id", "email", "region"] and doc["lineage"]["downstream"] == []
    if getattr(s, "ask", None) is None:
        pytest.skip("Ask-your-data is not built in this configuration")
    rc, out, err = cli(home, "ask", "how", "many", "orders", "per", "region", "--dataset", ORDERS)
    assert rc == 0, err
    assert "SELECT region, count(*) AS n FROM sales.orders" in out and "EU" in out and "US" in out
    rc, out, _ = cli(home, "ask", "orders per region", "--dataset", ORDERS, "--output", "json", "--profile", "bob")
    answer = json.loads(out)
    assert rc == 0 and answer["result"]["rows"] == [["EU", 2]]                     # bob's row filter
    assert ModelProvider.seen and "provider-key-held-by-cairn" not in out


def test_a_warehouse_needs_use_warehouse(stack, trino):  # noqa: F811
    """--warehouse: Cairn checks USE_WAREHOUSE first, then runs the statement on that warehouse's Trino (here the
    warehouse proxy's port for "team" leads to the test's Trino)."""
    from cairn.dataaccess.warehouse_status import WarehouseStatus

    url, s, home = stack
    before = s.statements.warehouses
    s.statements.warehouses = WarehouseStatus("https://127.0.0.1:1", {"team": trino.tls}, f"{trino.dir}/ca.pem")
    try:
        rc, _, err = cli(home, "query", Q, "--warehouse", "team")
        assert rc == 4 and "USE_WAREHOUSE on urn:li:warehouse:team" in err, err
        assert login(url, home, "admin", "urn:li:corpuser:admin")[0] == 0
        rc, out, err = cli(home, "query", Q, "--warehouse", "team", "--profile", "admin", "--format", "csv")
        assert rc == 0 and len(out.strip().splitlines()) == 4 and "jane@acme.com" in out, err
        assert cli(home, "query", Q, "--warehouse", "nope", "--profile", "admin")[0] == 5
        assert cli(home, "logout", "--profile", "admin")[0] == 0
    finally:
        s.statements.warehouses = before


def test_the_other_groups_use_the_signed_in_profile(stack, tmp_path):
    """flows, iac and ontology under the same verbs, as the person signed in (no CAIRN_URL/CAIRN_TOKEN needed)."""
    url, s, home = stack
    rc, out, err = cli(home, "flows", "list")
    assert rc == 0, err
    rc, _, err = cli(home, "iac", "export", "--format", "yaml", "--kind", "queries", "--out", str(tmp_path / "b.yaml"))
    assert rc == 0, err
    assert "bundle:" in (tmp_path / "b.yaml").read_text()
    rc, out, err = cli(home, "iac", "validate", str(tmp_path / "b.yaml"))
    assert rc == 0 and "valid" in out, out + err
    rc, out, err = cli(home, "iac", "plan", str(tmp_path / "b.yaml"))
    assert rc == 1 and "MANAGE_SYSTEM_OPERATIONS denied" in out + err      # as ana: a bundle's state is the operators'
    assert login(url, home, "admin", "urn:li:corpuser:admin")[0] == 0
    rc, out, err = cli(home, "iac", "plan", str(tmp_path / "b.yaml"), "--profile", "admin")
    assert rc == 0 and "to create" in out, out + err
    assert cli(home, "logout", "--profile", "admin")[0] == 0
    rc, out, err = cli(home, "ontology", "status")
    assert rc == 5 and "Cairn Ontology is not set up" in err, out + err           # not configured in this Cairn


def test_refresh_then_logout_and_revocation(stack):
    url, s, home = stack
    before = stored(home, "dev")
    # The access token is due: the next command refreshes it (rotating the refresh token) by itself.
    _rewrite(home, "dev", {**before, "access_expires": int(time.time()) - 5})
    rc, out, err = cli(home, "whoami", "--output", "json")
    after = stored(home, "dev")
    assert rc == 0 and json.loads(out)["user"] == ANA, err
    assert after["access_token"] != before["access_token"] and after["refresh_token"] != before["refresh_token"]
    old = httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {before['access_token']}"}, timeout=10)
    assert old.status_code == 200                                       # rotation does not end the old access token early
    # A spent refresh token coming back (it was copied) signs the device out.
    replay = httpx.post(f"{url}/oauth/token", data={"grant_type": "refresh_token", "client_id": "cairn-cli",
                                                    "refresh_token": before["refresh_token"]}, timeout=10)
    assert replay.json()["error"] == "invalid_grant"
    assert httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {after['access_token']}"}, timeout=10).status_code == 401
    rc, _, err = cli(home, "whoami")
    assert rc == 3 and "run `cairn login --profile dev`" in err
    # Sign in again; then sign out from the profile page (Signed-in devices): the CLI's tokens stop at once.
    assert login(url, home, "dev", ANA)[0] == 0
    token = stored(home, "dev")["access_token"]
    devices = httpx.get(f"{url}/v1/me/devices", headers={"X-Cairn-Actor": ANA}, timeout=10).json()["devices"]
    assert len(devices) == 1
    assert httpx.delete(f"{url}/v1/me/devices/{devices[0]['id']}", headers={"X-Cairn-Actor": BOB},
                        timeout=10).status_code == 404                  # not bob's to end
    assert httpx.delete(f"{url}/v1/me/devices/{devices[0]['id']}", headers={"X-Cairn-Actor": ANA},
                        timeout=10).status_code == 204
    assert httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {token}"}, timeout=10).status_code == 401
    rc, _, err = cli(home, "query", Q)
    assert rc == 3 and "cairn login" in err
    # `cairn logout`: Cairn ends the session, and nothing is left here.
    assert login(url, home, "bob", BOB)[0] == 0
    bob = stored(home, "bob")
    rc, _, err = cli(home, "logout", "--profile", "bob")
    assert rc == 0 and "Signed out" in err
    assert httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {bob['access_token']}"}, timeout=10).status_code == 401
    refreshed = httpx.post(f"{url}/oauth/token", data={"grant_type": "refresh_token", "client_id": "cairn-cli",
                                                       "refresh_token": bob["refresh_token"]}, timeout=10)
    assert refreshed.json()["error"] == "invalid_grant"
    rc, _, err = cli(home, "whoami", "--profile", "bob")
    assert rc == 3 and "not signed in" in err
    assert any(e.get("action") == "device.signed_out" for e in s.audit.entries("identity", 50))


def test_token_stdin_env_precedence_and_the_0600_file(stack, monkeypatch, tmp_path_factory):
    url, s, home = stack
    pats = {"pat-cli-ana-" + "a" * 16: ANA, "pat-cli-bob-" + "b" * 16: BOB}
    real = s.identity._me_lookup
    monkeypatch.setattr(s.identity, "_me_lookup", lambda u, h, c: pats.get((h.get("Authorization") or "")[7:]) or real(u, h, c))
    ana_pat, bob_pat = list(pats)
    # --token-stdin, for CI: checked against Cairn, kept like a sign-in, never echoed.
    rc, out, err = cli(home, "login", "--url", url, "--profile", "ci", "--token-stdin", input=ana_pat + "\n")
    assert rc == 0 and ana_pat not in out + err, err
    assert json.loads(cli(home, "whoami", "--profile", "ci", "--output", "json")[1])["via"] == "token"
    rc, _, err = cli(home, "login", "--url", url, "--profile", "ci2", "--token-stdin", input="pat-wrong\n")
    assert rc == 3 and "refused the token" in err and "pat-wrong" not in err
    # CAIRN_URL and CAIRN_TOKEN win over every profile (CI), even over --profile.
    rc, out, _ = cli(home, "whoami", "--output", "json", "--profile", "ci", CAIRN_URL=url, CAIRN_TOKEN=bob_pat)
    me = json.loads(out)
    assert rc == 0 and me["user"] == BOB and me["source"] == "environment" and me["profile"] is None
    rc, out, _ = cli(home, "query", Q, "--format", "csv", CAIRN_URL=url, CAIRN_TOKEN=bob_pat)
    assert rc == 0 and len(out.strip().splitlines()) == 3                               # bob's EU rows
    tf = tmp_path_factory.mktemp("tok") / "token"
    tf.write_text(bob_pat)
    assert json.loads(cli(home, "whoami", "--output", "json", CAIRN_URL=url, CAIRN_TOKEN_FILE=str(tf))[1])["user"] == BOB
    # A profile's tokens never go to another Cairn named in CAIRN_URL.
    rc, _, err = cli(home, "whoami", CAIRN_URL="http://127.0.0.1:9")
    assert rc == 3 and "set CAIRN_TOKEN" in err
    # No keychain: the tokens go to a 0600 file, with a warning; a file others can read is refused.
    bare = tmp_path_factory.mktemp("bare")
    rc, _, err = cli(bare, "login", "--url", url, "--token-stdin", input=ana_pat + "\n",
                     PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    cred_file = bare / "config" / "credentials.json"
    assert rc == 0 and "no OS keychain available" in err and str(cred_file) in err, err
    assert stat.S_IMODE(cred_file.stat().st_mode) == 0o600 and stat.S_IMODE(cred_file.parent.stat().st_mode) == 0o700
    assert json.loads(cli(bare, "whoami", "--output", "json", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")[1])["user"] == ANA
    os.chmod(cred_file, 0o644)
    rc, _, err = cli(bare, "whoami", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    assert rc == 3 and "can be read by others" in err and "chmod 600" in err


def test_the_sql_editor_runs_page_by_page_with_history_cancel_and_plans(stack, trino):  # noqa: F811
    """The SQL editor on real Trino (L163 part 3): a statement started from the editor is one history entry, running
    while its pages are read and finished with its rows and time once the last page is; a stopped one is cancelled in
    Trino and in the history; a plan is the engine's EXPLAIN of the statement as the person runs it (their row filter
    in it), refused for a table they may not read, and nothing runs for it."""
    url, s, _ = stack
    ana, bob = {"X-Cairn-Actor": ANA}, {"X-Cairn-Actor": BOB}
    hist = lambda h: httpx.get(f"{url}/sql/history", headers=h, timeout=10).json()  # noqa: E731
    page = httpx.post(f"{url}/v1/statements", json={"sql": BIG, "connection": "lakehouse", "history": True},
                      headers=ana, timeout=60).json()
    assert hist(ana)[0]["status"] == "running" and hist(ana)[0]["sql"] == BIG
    rows = len(page.get("rows") or [])
    while page.get("next"):
        page = httpx.post(f"{url}/v1/statements/next", json={"cursor": page["next"]}, headers=ana, timeout=60).json()
        rows += len(page.get("rows") or [])
    entry = hist(ana)[0]
    assert rows == 15000 and entry["status"] == "ok" and entry["rows"] == 15000 and entry["ms"] >= 0, entry
    # A long statement stopped: Bob cannot use Ana's cursor (so nor end her history entry); Ana's stop cancels it in
    # Trino and the entry says so.
    started = httpx.post(f"{url}/v1/statements", json={"sql": ENDLESS, "connection": "lakehouse", "history": True},
                         headers=ana, timeout=60).json()
    while not started.get("rows"):                                # running in Trino: rows are arriving
        started = httpx.post(f"{url}/v1/statements/next", json={"cursor": started["next"]}, headers=ana,
                             timeout=60).json()
    assert httpx.post(f"{url}/v1/statements/cancel", json={"cursor": started["next"]}, headers=bob,
                      timeout=60).status_code == 403
    assert hist(ana)[0]["status"] == "running"
    stopped = httpx.post(f"{url}/v1/statements/cancel", json={"cursor": started["next"]}, headers=ana, timeout=60)
    assert stopped.json()["cancelled"] is True
    assert hist(ana)[0]["status"] == "cancelled" and hist(ana)[0]["error"] == "Stopped"
    end = time.time() + 60
    while True:
        got = trino(f"SELECT state, error_code FROM system.runtime.queries WHERE query_id = '{started['id']}'")
        if got and got[0]["state"] == "FAILED" or time.time() > end:
            break
        time.sleep(1)
    assert got and got[0]["error_code"] == "USER_CANCELED", got
    # Without "history" (cairn query) nothing is added to the history.
    before = len(hist(ana))
    httpx.post(f"{url}/v1/statements", json={"sql": Q, "connection": "lakehouse", "limit": 1}, headers=ana, timeout=60)
    assert len(hist(ana)) == before
    # Plans: Trino's distributed plan of the statement as the person runs it; Bob's carries his EU filter.
    plan = httpx.post(f"{url}/sql/explain", json={"sql": Q, "connection": "lakehouse"}, headers=bob, timeout=60)
    assert plan.status_code == 200, plan.text
    p = plan.json()
    assert "Fragment" in p["plan"] and p["dialect"] == "trino" and p["tables"] == [ORDERS]
    assert "'EU'" in p["plan"]                                     # his row filter (from Cairn's policy) is planned
    assert "'EU'" not in httpx.post(f"{url}/sql/explain", json={"sql": Q, "connection": "lakehouse"}, headers=ana,
                                    timeout=60).json()["plan"]
    runs = len(hist(bob))
    denied = httpx.post(f"{url}/sql/explain", json={"sql": "SELECT * FROM hr.salaries", "connection": "lakehouse"},
                        headers=bob, timeout=60)
    assert denied.status_code == 403 and "may not query" in denied.text
    assert len(hist(bob)) == runs                                     # plans are not runs
    assert any(e["action"] == "data.explained" and e["actor"] == BOB for e in s.audit.entries("data", 50))
