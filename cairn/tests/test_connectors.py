"""Connector syncs (L165, part 2): what is checked, refused and kept secret, and the limits and the network of a run.

The connector is a real image built here with Docker (``tests/connectors/standin-source``: the Airbyte protocol, told
to misbehave by its config), pushed to a real registry and pulled by its digest; the secrets are in a real secret
scope. It is a stand-in, not a vendor's connector. Landing in tables (the store, the catalog, Trino) is
``tests/test_connectors_e2e.py``. The tests that start containers are skipped without Docker."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from connector_standin import docker_ready, standin_image  # noqa: F401  (fixture)

OWNER, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:bob"
SECRET = "sk-live-" + "s" * 24
FAKE = "registry.example.com/airbyte/source-faker@sha256:" + "a" * 64
needs_docker = pytest.mark.skipif(not docker_ready(), reason="needs docker")


def make(tmp_path, monkeypatch, image, runtime=None, env=None):
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.pipelines.connectors import ConnectorSyncs

    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    if image:
        monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", f"{image}, registry.example.com/other@sha256:{'b' * 64}")
    monkeypatch.setenv("CAIRN_CONNECTOR_ALLOWED_HOSTS", "api.example.com,127.0.0.1")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    engine = create_engine(f"sqlite:///{tmp_path}/connectors.db")
    init_db(engine)
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    scopes.create(OWNER, {"name": "source-keys"})
    scopes.put(OWNER, "source-keys", "api_key", SECRET)
    audited, told = [], []

    class Audit:
        def record(self, *a):
            audited.append(a)
    syncs = ConnectorSyncs(engine, {}, scopes.read, lambda owner, urn: owner == OWNER and "forbidden" not in urn,
                           runtime, audit=Audit(), notify=lambda *a: told.append(a))
    return {"syncs": syncs, "scopes": scopes, "audited": audited, "told": told, "engine": engine, "image": image}


def body(image, **over):
    return {"name": "customers", "image": image, "config": {"api_key": {"secret": "api_key"}, "count": 5},
            "secret_scope": "source-keys", "streams": [{"name": "customers", "target": "sales.cx_customers", "mode": "upsert"}],
            **over}


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """The service with an allowed image that is never run (no runtime): what is checked without a container."""
    return make(tmp_path, monkeypatch, FAKE)


@pytest.fixture
def world(tmp_path, monkeypatch, standin_image):  # noqa: F811
    from cairn.pipelines.connectors import DockerConnectorRuntime

    return make(tmp_path, monkeypatch, standin_image, DockerConnectorRuntime(allow_private=True, host_firewall="off"))


def everything(w, caplog=None) -> str:
    """All that is kept or told about the syncs: the stored rows, the audit records, the notices, the log."""
    from sqlalchemy import select

    from cairn.pipelines.connectors import connector_syncs

    with w["engine"].connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(connector_syncs))]
    return json.dumps([rows, w["audited"], w["told"], caplog.text if caplog is not None else ""], default=str)


# ---- without a container ---------------------------------------------------------------------------------------
def test_without_the_operator_s_list_every_route_answers_503(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.pipelines.connectors import ConnectorSyncs, build_connectors_router

    monkeypatch.delenv("CAIRN_CONNECTOR_IMAGES", raising=False)
    w = make(tmp_path, monkeypatch, None)
    app = FastAPI()
    app.include_router(build_connectors_router(w["syncs"], lambda: SimpleNamespace(urn=OWNER)))
    http = TestClient(app)
    calls = [("get", "/v1/connector-syncs", None), ("post", "/v1/connector-syncs", body(FAKE)),
             ("get", "/v1/connector-syncs/customers", None), ("put", "/v1/connector-syncs/customers", body(FAKE)),
             ("delete", "/v1/connector-syncs/customers", None), ("post", "/v1/connector-syncs/customers/run", None),
             ("post", "/v1/connector-syncs/spec", {"image": FAKE}), ("post", "/v1/connector-syncs/check", {"image": FAKE}),
             ("post", "/v1/connector-syncs/discover", {"image": FAKE})]
    for method, path, sent in calls:
        r = http.request(method, path, json=sent)
        assert r.status_code == 503 and "CAIRN_CONNECTOR_IMAGES" in r.text, (method, path, r.status_code, r.text)
    assert w["syncs"].run_due(wait=True) == 0
    # A tag in the operator's list is a mistake that allows nothing: Cairn does not start with it.
    monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", "airbyte/source-faker:latest")
    with pytest.raises(ValueError, match="sha256"):
        ConnectorSyncs(w["engine"], {}, w["scopes"].read, lambda *a: True, None)


def test_a_sync_is_checked_when_saved(plain):
    from cairn.pipelines.connectors import ConnectorError

    syncs = plain["syncs"]
    digest = "c" * 64
    for change, why, status in (
            ({"image": "airbyte/source-faker:6.2.0"}, "digest", 400),                      # a tag
            ({"image": f"airbyte/source-faker:6.2.0@sha256:{digest}"}, "digest", 400),     # a tag beside the digest
            ({"image": f"airbyte/source-faker@sha256:{digest}"}, "CAIRN_CONNECTOR_IMAGES", 403),   # not on the list
            ({"image": FAKE.upper()}, "digest", 400),
            ({"config": {"api_key": SECRET}}, "secret is not written", 400),               # a literal secret
            ({"config": {"db": {"Password": "hunter2"}}}, "secret is not written", 400),
            ({"config": {"credentials": [{"client_secret": "x"}]}}, "secret is not written", 400),
            ({"config": {"api_key": {"secret": "no such/key"}}}, "names a key", 400),
            ({"secret_scope": None}, "secret_scope", 400),                                  # a reference, and no scope
            ({"config": []}, "config", 400),
            ({"streams": []}, "streams", 400),
            ({"streams": [{"name": "customers", "target": "Sales.Customers"}]}, "target", 400),
            ({"streams": [{"name": "customers", "target": "sales.c", "mode": "merge"}]}, "mode", 400),
            ({"streams": [{"name": "customers", "target": "sales.c", "table": "x"}]}, "streams", 400),
            ({"streams": [{"name": "a", "target": "sales.c"}, {"name": "b", "target": "sales.c"}]}, "twice", 400),
            ({"streams": [{"name": "a", "target": "sales.c", "mode": "replace", "sync_mode": "incremental"}]}, "whole", 400),
            ({"streams": [{"name": "a", "target": "sales.c", "primary_key": []}]}, "primary_key", 400),
            ({"egress": ["api.example.com", "evil.example.org"]}, "evil.example.org", 403),
            ({"egress": ["*.example.com"]}, "egress", 400),
            ({"egress": ["http://api.example.com/"]}, "egress", 400),
            ({"limits": {"timeout_s": 10 ** 9}}, "timeout_s", 400),
            ({"limits": {"memory": 1}}, "limits", 400),
            ({"cron": "every day"}, "cron", 400),
            ({"name": "Customers"}, "name", 400),
            ({"streams": [{"name": "customers", "target": "sales.forbidden"}]}, "WRITE_DATASET", 403)):
        with pytest.raises(ConnectorError, match=why) as e:
            syncs.save(OWNER, {**body(FAKE), **change})
        assert e.value.status == status, (change, str(e.value))
        assert SECRET not in str(e.value)
    with pytest.raises(ConnectorError) as e:                 # someone who may not write the table
        syncs.save(BOB, body(FAKE))
    assert e.value.status == 403

    saved = syncs.save(OWNER, body(FAKE, egress=["API.example.com:8443", "api.example.com"], cron="0 * * * *",
                                   limits={"timeout_s": 60}))
    assert saved["config"] == {"api_key": {"secret": "api_key"}, "count": 5}        # the reference, never the value
    assert saved["egress"] == ["api.example.com", "api.example.com:8443"] and saved["next_run_ms"]
    assert saved["has_state"] is False and "state" not in saved and saved["limits"] == {"timeout_s": 60}
    with pytest.raises(ConnectorError) as e:
        syncs.save(OWNER, body(FAKE))
    assert e.value.status == 409
    # Only its owner sees, changes, runs or deletes it.
    assert syncs.list(BOB) == [] and [s["name"] for s in syncs.list(OWNER)] == ["customers"]
    for call in (lambda: syncs.get(BOB, "customers"), lambda: syncs.save(BOB, body(FAKE), "customers"),
                 lambda: syncs.delete(BOB, "customers"), lambda: syncs.run_now(BOB, "customers"),
                 lambda: syncs.check(BOB, {"sync": "customers"})):
        with pytest.raises(ConnectorError) as e:
            call()
        assert e.value.status == 404
    # Changing the streams starts from the beginning; changing the config or the image does not.
    from sqlalchemy import update

    from cairn.pipelines.connectors import connector_syncs

    with plain["engine"].begin() as conn:
        conn.execute(update(connector_syncs).values(state='{"legacy": {"seq": 3}}'))
    assert syncs.save(OWNER, body(FAKE, config={"api_key": {"secret": "api_key"}, "count": 9}), "customers")["has_state"]
    assert not syncs.save(OWNER, body(FAKE, streams=[{"name": "customers", "target": "sales.cx_customers"}]),
                          "customers")["has_state"]
    assert SECRET not in everything(plain)
    assert [a[1] for a in plain["audited"] if a[1].startswith("connector.")] == ["connector.sync_saved"] * 3
    syncs.delete(OWNER, "customers")
    assert syncs.list(OWNER) == []


def test_nothing_runs_without_a_runtime(plain, monkeypatch):
    from cairn.pipelines.connectors import ConnectorError, runtime_from_env

    syncs = plain["syncs"]
    syncs.save(OWNER, body(FAKE, cron="* * * * *"))
    for call in (lambda: syncs.run_now(OWNER, "customers", wait=True), lambda: syncs.spec(OWNER, {"image": FAKE}),
                 lambda: syncs.check(OWNER, {"sync": "customers"}), lambda: syncs.discover(OWNER, {"sync": "customers"})):
        with pytest.raises(ConnectorError, match="CAIRN_CONNECTOR_RUNTIME") as e:
            call()
        assert e.value.status == 503
    # A scheduled run fails, on the record, and its owner is told: it is never run some other way.
    assert syncs.run_due(now=syncs.get(OWNER, "customers")["next_run_ms"] + 1, wait=True) == 1
    sync = syncs.get(OWNER, "customers")
    assert sync["last_status"] == "failed" and "CAIRN_CONNECTOR_RUNTIME" in sync["last_error"] and not sync["running"]
    assert plain["told"][-1][0] == "saas.sync_failed" and plain["told"][-1][3] == [OWNER]
    monkeypatch.delenv("CAIRN_CONNECTOR_RUNTIME", raising=False)
    assert runtime_from_env() is None
    monkeypatch.setenv("CAIRN_CONNECTOR_RUNTIME", "kubernetes")          # not built: refused, not ignored
    with pytest.raises(ValueError):
        runtime_from_env()
    monkeypatch.setenv("CAIRN_CONNECTOR_RUNTIME", "docker")
    monkeypatch.setenv("CAIRN_CONNECTOR_USER", "airbyte")
    with pytest.raises(ValueError):
        runtime_from_env()


def test_secrets_are_resolved_and_removed_from_what_is_said():
    from cairn.pipelines.connectors import redact, redact_all, resolve_config, secret_refs, state_file

    config = {"api_key": {"secret": "k1"}, "nested": [{"token": {"secret": "k2"}}], "count": 3, "secret": "plain"}
    assert sorted(secret_refs({"a": config["api_key"], "b": config["nested"]})) == ["k1", "k2"]
    resolved, values = resolve_config(config, {"k1": 'p"w\\d', "k2": "tok-123"}.__getitem__)
    assert resolved == {"api_key": 'p"w\\d', "nested": [{"token": "tok-123"}], "count": 3, "secret": "plain"}
    said = redact('refused: p"w\\d and ' + json.dumps({"key": 'p"w\\d'}) + " and tok-123", values)
    assert 'p"w' not in said and "tok-123" not in said and said.count("***") == 3
    assert redact_all({"m": ["tok-123 x"], "n": 1}, values) == {"m": ["*** x"], "n": 1}
    assert state_file(None) is None and json.loads(state_file({"legacy": {"seq": 2}})) == {"seq": 2}
    one = {"type": "STREAM", "stream": {"stream_descriptor": {"name": "c"}, "stream_state": {"seq": 2}}}
    assert json.loads(state_file({"streams": {"k": one}})) == [one]


# ---- with the stand-in connector's image -------------------------------------------------------------------------
@needs_docker
def test_spec_check_and_discover_run_the_image_and_echo_no_secret(world, caplog):
    from cairn.pipelines.connectors import ConnectorError

    syncs, image = world["syncs"], world["image"]
    caplog.set_level(logging.DEBUG)
    spec = syncs.spec(BOB, {"image": image})["spec"]              # no config, no secrets: anyone signed in
    assert spec["connectionSpecification"]["properties"]["api_key"]["airbyte_secret"] is True
    draft = {"image": image, "config": {"api_key": {"secret": "api_key"}}, "secret_scope": "source-keys"}
    assert syncs.check(OWNER, draft) == {"status": "SUCCEEDED", "message": "ok"}
    catalog = syncs.discover(OWNER, draft)["catalog"]
    assert [s["name"] for s in catalog["streams"]] == ["customers", "regions"]
    assert catalog["streams"][0]["source_defined_primary_key"] == [["id"]]
    # A connector that repeats the key it was given: the answer has it removed.
    world["scopes"].put(OWNER, "source-keys", "bad_key", "wrong-" + SECRET)
    refused = syncs.check(OWNER, {**draft, "config": {"api_key": {"secret": "bad_key"}}})
    assert refused == {"status": "FAILED", "message": "the key *** was refused"}
    # The keys are read as the caller: someone who may not read the scope gets nothing, and no container starts.
    before = len(world["audited"])
    with pytest.raises(ConnectorError, match="cannot read api_key") as e:
        syncs.check(BOB, draft)
    assert e.value.status == 403 and world["audited"][before:][-1][1] == "connector.check_failed"
    with pytest.raises(ConnectorError, match="cannot read missing") as e:
        syncs.discover(OWNER, {**draft, "config": {"api_key": {"secret": "missing"}}})
    # A saved sync's, by its name.
    syncs.save(OWNER, body(image))
    assert syncs.check(OWNER, {"sync": "customers"})["status"] == "SUCCEEDED"
    assert SECRET not in everything(world, caplog)
    assert {a[1] for a in world["audited"]} >= {"connector.spec", "connector.check", "connector.discover"}
    assert not os.popen("docker ps -aq --filter name=cairn-cx-").read().strip()       # nothing left behind


@needs_docker
def test_a_run_s_limits_and_the_protocol_are_enforced(world, caplog):
    syncs, image = world["syncs"], world["image"]
    caplog.set_level(logging.DEBUG)

    def run(misbehave, **over):
        config = {"api_key": {"secret": "api_key"}, "count": 5, "misbehave": misbehave}
        saved = syncs.save(OWNER, body(image, config=config, **over), "customers" if syncs.list(OWNER) else None)
        assert saved["config"]["misbehave"] == misbehave
        return syncs.run_now(OWNER, "customers", wait=True)
    syncs.max_line = 2000
    out = run({"long_line": 5000})
    assert out["status"] == "failed" and "line over 2000 bytes" in out["error"], out
    out = run({"not_json": True})
    assert out["status"] == "failed" and "line 2 of the connector's output is not a protocol message" in out["error"]
    assert "this line" not in out["error"]                                # what it printed is not repeated
    out = run({"unknown_stream": True})
    assert out["status"] == "failed" and "a stream the sync did not ask for" in out["error"]
    syncs.max_records = 40
    out = run({"flood": True})
    assert out["status"] == "failed" and "over 40 records" in out["error"] and out["rows"] == 0
    syncs.max_records, syncs.max_output = 10 ** 9, 20_000
    out = run({"flood": True})
    assert out["status"] == "failed" and "over 20000 bytes" in out["error"]
    syncs.max_output = 1 << 30
    out = run({"sleep": 60}, limits={"timeout_s": 3})
    assert out["status"] == "failed" and "time limit (3 s)" in out["error"]
    # An error the connector reports fails the run even when its process ends well; of what it said, only the
    # last error's message is kept, cut to 300 characters.
    out = run({"error_trace": True}, streams=[{"name": "regions", "target": "sales.cx_regions", "mode": "replace"}])
    assert out["status"] == "failed" and out["error"].endswith("reported an error: half the pages failed"), out
    sync = syncs.get(OWNER, "customers")
    assert sync["last_status"] == "failed" and sync["last_stats"]["traces"] >= 1 and sync["last_stats"]["logs"] == 1
    assert world["told"][-1][0] == "saas.sync_failed" and world["told"][-1][3] == [OWNER]
    assert world["audited"][-1][1] == "connector.sync_failed" and world["audited"][-1][2] == OWNER
    # The operator's lists are checked again at every run: an image or a host taken off them no longer runs.
    syncs.save(OWNER, body(image, egress=["api.example.com"]), "customers")
    syncs.allowed_hosts = set()
    assert "CAIRN_CONNECTOR_ALLOWED_HOSTS" in syncs.run_now(OWNER, "customers", wait=True)["error"]
    syncs.images = {FAKE}
    assert "CAIRN_CONNECTOR_IMAGES" in syncs.run_now(OWNER, "customers", wait=True)["error"]
    syncs.images, syncs.allowed_hosts = {image}, {"api.example.com"}
    world["scopes"].remove(OWNER, "source-keys", "api_key")              # a secret that cannot be read: no run
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed" and "cannot read api_key" in out["error"]
    assert SECRET not in everything(world, caplog)
    assert not os.popen("docker ps -aq --filter name=cairn-cx-").read().strip()
    assert "cairn-cx-" not in os.popen("docker network ls --format '{{.Name}}'").read()


@needs_docker
def test_what_a_connector_prints_about_its_config_is_never_kept(world, caplog, capfd):
    syncs, image = world["syncs"], world["image"]
    caplog.set_level(logging.DEBUG)
    # It logs its whole config (on stdout as a LOG and on stderr), then fails with the key in its error message and
    # the config in the stack trace.
    syncs.save(OWNER, body(image, config={"api_key": {"secret": "api_key"}, "count": 5,
                                          "misbehave": {"print_config": "error", "crash_after": 0}}))
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed", out
    assert out["error"] == "the connector's read exited with status 1: the source went away (key ***)"
    assert out["stats"]["logs"] == 2 and out["stats"]["traces"] == 1 and out["stats"]["records"] == 0
    sync = syncs.get(OWNER, "customers")
    assert sync["last_error"] == out["error"] and sync["config"]["api_key"] == {"secret": "api_key"}
    kept = everything(world, caplog) + json.dumps(out) + json.dumps(sync) + "".join(capfd.readouterr())
    assert SECRET not in kept and "config: " not in kept
    assert world["told"][-1][2] == out["error"]
    # A long message is cut to 300 characters (after the secret is taken out of it).
    from cairn.pipelines.connectors import ConnectorJob

    syncs.save(OWNER, body(image, config={"api_key": {"secret": "api_key"}, "count": 1, "error_note": ": " + "y" * 900,
                                          "misbehave": {"print_config": "error", "crash_after": 0}}), "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    head = "the connector's read exited with status 1: "
    assert out["error"].startswith(head + "the source went away: yyy") and len(out["error"]) == len(head) + 300
    # The command line names the files, never what is in them; the files are gone with the run.
    job = ConnectorJob(image, "read", {"config.json": b"{}", "catalog.json": b"{}"}, [], 10, 64, 1, 100, 100)
    assert job.args() == ["read", "--config", "/cairn/config.json", "--catalog", "/cairn/catalog.json"]
    import glob
    import tempfile

    assert glob.glob(os.path.join(tempfile.gettempdir(), "cairn-cx-*")) == []


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"allowed")


def _serve(host):
    server = ThreadingHTTPServer((host, 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _probe(syncs, image, probes, egress):
    out = syncs.check(OWNER, {"image": image, "config": {"api_key": {"secret": "api_key"}, "probe": probes},
                              "secret_scope": "source-keys", "egress": egress})
    assert out["status"] == "SUCCEEDED", out
    return json.loads(out["message"])


@needs_docker
def test_a_container_reaches_only_the_hosts_the_sync_names_through_the_proxy(world):
    """The run's network is internal: no route out, no names resolved for it. The one way out is the proxy at its
    gateway, which lets through the sync's hosts only."""
    syncs, image = world["syncs"], world["image"]
    allowed, other, everywhere = _serve("127.0.0.1"), _serve("127.0.0.1"), _serve("0.0.0.0")
    host = os.popen("docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'").read().strip()
    try:
        probes = [{"url": f"http://127.0.0.1:{allowed.server_port}/"},        # the sync's host: through the proxy
                  {"url": f"http://127.0.0.1:{other.server_port}/"},          # another port of it: refused
                  {"url": "http://example.com/"},                              # another host: refused
                  {"direct": f"{host}:{everywhere.server_port}"},              # the host, not through the proxy
                  {"direct": "1.1.1.1:80"}, {"resolve": "example.com"}]
        got = _probe(syncs, image, probes, [f"127.0.0.1:{allowed.server_port}"])
        assert got[0] == {"url": probes[0]["url"], "status": 200, "body": "allowed"}, got
        assert got[1]["status"] == 403 and got[2]["status"] == 403, got
        assert got[3]["open"] is False and got[4]["open"] is False and got[5]["resolved"] is False, got
        # A private address is refused by the proxy unless the operator allows them.
        syncs.runtime.allow_private = False
        assert _probe(syncs, image, probes[:1], [f"127.0.0.1:{allowed.server_port}"])[0]["status"] == 403
        syncs.runtime.allow_private = True
        # Without egress there is no network at all: no proxy, no route.
        got = _probe(syncs, image, probes, [])
        assert [g.get("status") for g in got[:3]] == [None, None, None], got
        assert got[3]["open"] is False and got[4]["open"] is False and got[5]["resolved"] is False, got
        # A host the operator does not list is refused before anything runs.
        from cairn.pipelines.connectors import ConnectorError

        with pytest.raises(ConnectorError, match="CAIRN_CONNECTOR_ALLOWED_HOSTS") as e:
            _probe(syncs, image, probes, ["evil.example.org"])
        assert e.value.status == 403
        # Until the operator has chosen how the host is protected from the run's network, a sync with egress does not
        # run at all (and one without egress still does).
        syncs.runtime.host_firewall = ""
        with pytest.raises(ConnectorError, match="CAIRN_CONNECTOR_HOST_FIREWALL") as e:
            _probe(syncs, image, probes[:1], [f"127.0.0.1:{allowed.server_port}"])
        assert e.value.status == 503
        assert _probe(syncs, image, probes[:1], [])[0].get("status") is None
        syncs.runtime.host_firewall = "off"
        # The gateway is the host's address: what the host serves on every interface answers there (unless the
        # host's own firewall already stops it, so that is not asserted)...
        gateway = [{"gateway_port": everywhere.server_port}]
        assert isinstance(_probe(syncs, image, gateway, [f"127.0.0.1:{allowed.server_port}"])[0]["open"], bool)
        # ...unless Cairn sets the host's firewall for the run's network (it needs the right to: root here).
        if os.geteuid() == 0 and shutil.which("iptables"):
            syncs.runtime.host_firewall = "iptables"
            got = _probe(syncs, image, gateway + probes[:1], [f"127.0.0.1:{allowed.server_port}"])
            assert got[0]["open"] is False and got[1]["status"] == 200, got
            assert "cx" not in os.popen("iptables -S INPUT").read()          # the rules go with the run
        else:
            # Without the right, the run is refused rather than run without the rule.
            syncs.runtime.host_firewall = "iptables"
            with pytest.raises(ConnectorError, match="firewall") as e:
                _probe(syncs, image, gateway, [f"127.0.0.1:{allowed.server_port}"])
            assert e.value.status == 503
        assert "cairn-cx-" not in os.popen("docker network ls --format '{{.Name}}'").read()
    finally:
        for s in (allowed, other, everywhere):
            s.shutdown()
