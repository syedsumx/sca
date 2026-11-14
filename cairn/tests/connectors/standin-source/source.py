"""A stand-in source connector for Cairn's tests. It speaks the Airbyte protocol on stdout (one JSON message a line):

    source.py spec
    source.py check    --config config.json
    source.py discover --config config.json
    source.py read     --config config.json --catalog catalog.json [--state state.json]

Two streams: ``customers`` (incremental by ``seq``, primary key ``id``; a per-stream STATE after every ``page``
records) and ``regions`` (full refresh, no key). The data comes from the config, so a test changes "the source" by
changing the sync's config: ``count`` customers, then ``changes`` (``[[id, name], ...]``: a customer changed later, so
it has a newer ``seq``), and ``regions`` (a list of names). ``workers`` makes the read start worker processes first.

``misbehave`` makes it do what a connector should not: ``{"long_line": bytes}``, ``{"crash_after": records}``,
``{"print_config": "log" | "error"}``, ``{"not_json": true}``, ``{"sleep": seconds}``, ``{"flood": true}``,
``{"unknown_stream": true}``, ``{"legacy_state": true}``, ``{"error_trace": true}``. ``probe`` (check only) is a
list of ``{"direct": "host:port"}`` (a plain TCP connection), ``{"url": "http://..."}`` (through the proxy the
environment names), ``{"gateway_port": port}`` (a plain TCP connection to the proxy's address, on another port) or
``{"resolve": "name"}``; what each did comes back in the connection status's message, as JSON.

Standard library only."""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

SPEC = {"documentationUrl": "https://example.invalid/standin", "connectionSpecification": {
    "type": "object", "required": ["api_key"], "properties": {
        "api_key": {"type": "string", "airbyte_secret": True}, "count": {"type": "integer"},
        "page": {"type": "integer"}, "changes": {"type": "array"}, "regions": {"type": "array"},
        "misbehave": {"type": "object"}, "probe": {"type": "array"}}}}
CATALOG = {"streams": [
    {"name": "customers", "supported_sync_modes": ["full_refresh", "incremental"], "source_defined_cursor": True,
     "default_cursor_field": ["seq"], "source_defined_primary_key": [["id"]],
     "json_schema": {"type": "object", "properties": {
         "id": {"type": "integer"}, "Name": {"type": "string"}, "region": {"type": "string"},
         "seq": {"type": "integer"}, "vip": {"type": "boolean"}, "address": {"type": "object"},
         "tags": {"type": "array"}}}},
    {"name": "regions", "supported_sync_modes": ["full_refresh"],
     "json_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]}


def out(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def load(name):
    path = arg(name)
    if not path:
        return None
    with open(path) as fh:
        return json.load(fh)


def customers(config):
    """Every customer as the source has it now, oldest change first: (seq, record)."""
    count = int(config.get("count", 0))
    rows = {i: (i, f"Customer {i}") for i in range(1, count + 1)}
    for n, (cid, name) in enumerate(config.get("changes") or [], start=1):
        rows[int(cid)] = (count + n, name)
    for cid, (seq, name) in sorted(rows.items(), key=lambda kv: kv[1][0]):
        region = "EU" if cid % 2 else "US"
        rec = {"id": cid, "Name": name, "region": region, "seq": seq, "vip": cid % 3 == 0,
               "address": {"city": "Lyon" if region == "EU" else "Austin"}, "tags": ["a", cid]}
        if cid > 3 and config.get("extra_field"):
            rec["segment"] = "new"
        yield seq, rec


def probe(config):
    results = []
    for p in config.get("probe") or []:
        if "gateway_port" in p:                  # another port of the address the proxy is at (the host's)
            proxy = urllib.request.getproxies().get("http", "")
            p = {"direct": f"{proxy.rsplit('@', 1)[-1].rsplit(':', 1)[0]}:{p['gateway_port']}", "gateway": True}
        if "direct" in p:
            host, _, port = p["direct"].rpartition(":")
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect((host, int(port)))
                results.append({"direct": p["direct"], "open": True})
            except OSError as e:
                results.append({"direct": p["direct"], "open": False, "why": type(e).__name__})
            finally:
                s.close()
        elif "resolve" in p:
            try:
                socket.getaddrinfo(p["resolve"], 80)
                results.append({"resolve": p["resolve"], "resolved": True})
            except OSError:
                results.append({"resolve": p["resolve"], "resolved": False})
        else:
            try:
                with urllib.request.urlopen(p["url"], timeout=10) as r:
                    results.append({"url": p["url"], "status": r.status, "body": r.read(200).decode()})
            except urllib.error.HTTPError as e:
                results.append({"url": p["url"], "status": e.code})
            except OSError as e:
                results.append({"url": p["url"], "status": None, "why": type(e).__name__})
    return results


def read(config, catalog, state):
    bad = config.get("misbehave") or {}
    wanted = {s["stream"]["name"]: s for s in catalog["streams"]}
    # Where the last run stopped: per-stream state (a list of STATE messages) or the legacy blob.
    cursor = 0
    if isinstance(state, list):
        for st in state:
            if st.get("type") == "STREAM" and st["stream"]["stream_descriptor"]["name"] == "customers":
                cursor = int(st["stream"]["stream_state"].get("seq", 0))
    elif isinstance(state, dict):
        cursor = int(state.get("seq", 0))
    out({"type": "LOG", "log": {"level": "INFO", "message": f"reading from seq {cursor}"}})
    if config.get("workers"):                       # as connectors that read in worker processes do
        import multiprocessing

        with multiprocessing.Pool(int(config["workers"])) as pool:
            pool.map(abs, [-1, -2, -3])
    if bad.get("print_config"):
        out({"type": "LOG", "log": {"level": "INFO", "message": "config: " + json.dumps(config)}})
        print("config: " + json.dumps(config), file=sys.stderr)
    if bad.get("not_json"):
        print("this line is not a protocol message")
    if bad.get("long_line"):
        out({"type": "LOG", "log": {"level": "INFO", "message": "x" * int(bad["long_line"])}})
    if bad.get("sleep"):
        time.sleep(float(bad["sleep"]))
    if bad.get("unknown_stream"):
        out({"type": "RECORD", "record": {"stream": "invoices", "data": {"id": 1}, "emitted_at": 0}})
    emitted = 0

    def state_message(seq):
        if bad.get("legacy_state"):
            return {"type": "STATE", "state": {"data": {"seq": seq}}}
        return {"type": "STATE", "state": {"type": "STREAM", "stream": {
            "stream_descriptor": {"name": "customers"}, "stream_state": {"seq": seq}}}}
    if "regions" in wanted:
        for name in config.get("regions") or []:
            out({"type": "RECORD", "record": {"stream": "regions", "data": {"name": name}, "emitted_at": 0}})
    if "customers" in wanted:
        incremental = wanted["customers"].get("sync_mode") == "incremental"
        page, last = int(config.get("page", 2)), cursor
        while bad.get("flood"):
            emitted += 1
            out({"type": "RECORD", "record": {"stream": "customers", "data": {"id": emitted, "seq": emitted},
                                              "emitted_at": 0}})
        for seq, rec in customers(config):
            if incremental and seq <= cursor:
                continue
            if bad.get("crash_after") is not None and emitted >= int(bad["crash_after"]):
                message = "the source went away" + str(config.get("error_note") or "")
                if bad.get("print_config") == "error":
                    message += " (key " + str(config.get("api_key")) + ")"
                out({"type": "TRACE", "trace": {"type": "ERROR", "emitted_at": 0, "error": {
                    "message": message, "failure_type": "system_error", "stack_trace": json.dumps(config)}}})
                sys.exit(1)
            out({"type": "RECORD", "record": {"stream": "customers", "data": rec, "emitted_at": int(time.time() * 1000)}})
            emitted += 1
            last = seq
            if incremental and emitted % page == 0:
                out(state_message(last))
        if incremental:
            out(state_message(last))
    if bad.get("error_trace"):                      # an error reported, though the process ends well
        out({"type": "TRACE", "trace": {"type": "ERROR", "emitted_at": 0, "error": {"message": "half the pages failed"}}})
    out({"type": "TRACE", "trace": {"type": "STREAM_STATUS", "emitted_at": 0,
                                    "stream_status": {"stream_descriptor": {"name": "customers"}, "status": "COMPLETE"}}})


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "spec":
        return out({"type": "SPEC", "spec": SPEC})
    config = load("--config")
    if command == "check":
        if not config.get("api_key"):
            return out({"type": "CONNECTION_STATUS", "connectionStatus": {"status": "FAILED", "message": "no api_key"}})
        if config.get("api_key", "").startswith("wrong"):          # a connector that echoes what it was given
            return out({"type": "CONNECTION_STATUS", "connectionStatus": {
                "status": "FAILED", "message": f"the key {config['api_key']} was refused"}})
        message = json.dumps(probe(config)) if config.get("probe") else "ok"
        if os.environ.get("HTTP_PROXY") and not config.get("probe"):
            message = "ok (through a proxy)"
        return out({"type": "CONNECTION_STATUS", "connectionStatus": {"status": "SUCCEEDED", "message": message}})
    if command == "discover":
        return out({"type": "CATALOG", "catalog": CATALOG})
    if command == "read":
        return read(config, load("--catalog"), load("--state"))
    print(f"unknown command {command}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
