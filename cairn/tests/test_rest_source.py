"""The declarative REST source (L165): a spec is checked, and records are read from a real HTTP API on a local port
with each pagination style and each way of sending a key; what Cairn refuses to fetch is refused. (Records landing
in a governed lakehouse table are in test_rest_source_e2e.py.)"""

import socket
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from rest_api_standin import Api, customer, serve  # noqa: E402

from cairn.pipelines import rest_source  # noqa: E402
from cairn.pipelines.rest_source import RestSpecError, check_spec, rest_records  # noqa: E402
from cairn.pipelines.saas import SaasError, _public  # noqa: E402


@pytest.fixture
def api():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    a = Api()
    for i, day in enumerate([1, 1, 2, 2, 3]):
        a.customers[str(i)] = customer(i, day, status="active" if i != 3 else "closed", city="Lyon" if i % 2 else "Oslo")
    server = serve(a, port)
    a.base = f"http://127.0.0.1:{port}"
    yield a
    server.shutdown()


def allow(url, what):                                       # the local API is on this machine: allowed here only
    return url


def read(api, style, spec, cursor=None, secrets=None, fields=("name", "address.city", "tags", "vip"), check=allow):
    secrets = {"base_url": f"{api.base}/{style}", "token": api.token, **(secrets or {})}
    slept = []
    with httpx.Client(timeout=10) as http:
        out = list(rest_records(http, secrets, "customers", list(fields), check_spec(spec), cursor, check,
                                sleep=slept.append, error=SaasError))
    return out, slept


CURSOR = {"records": "data.items", "modified": "updated_at", "cursor_param": "updated_since",
          "pagination": {"type": "cursor", "param": "starting_after", "path": "meta.next"}}


def test_every_pagination_style_reads_all_records(api):
    specs = {
        "bearer-none": {"records": ""},
        "bearer-cursor": CURSOR,
        "bearer-offset": {"records": "results", "pagination": {"type": "offset", "limit": 2}},
        "bearer-page": {"records": "results", "pagination": {"type": "page", "size": 2}},
        "bearer-link": {"records": "results", "pagination": {"type": "link"}},
        "bearer-bodylink": {"records": "results", "pagination": {"type": "link", "path": "next"}},
    }
    for style, spec in specs.items():
        api.asked.clear()
        got, _ = read(api, style, spec)
        assert [r["id"] for r in got] == [f"cus_{i:03d}" for i in range(5)], style
        assert len(api.asked) == (1 if style == "bearer-none" else 3), (style, api.asked)
    first = got[0]
    assert first["values"] == {"name": "Customer 0", "address.city": "Oslo", "tags": '["a","b"]', "vip": "true"}
    assert rest_source.column("address.city") == "address_city"
    # Incremental: the cursor is sent as the parameter the spec names; fixed parameters go with every page.
    got, _ = read(api, "bearer-cursor", {**CURSOR, "params": {"status": "active"}}, cursor="2026-11-02T10:00:00Z")
    assert [r["id"] for r in got] == ["cus_002", "cus_004"] and got[0]["modified"] == "2026-11-02T10:00:00Z"
    assert all("updated_since=2026-11-02" in a and "status=active" in a for a in api.asked[-1:])


def test_each_way_of_sending_the_key(api):
    page = {"records": "results", "pagination": {"type": "link"}}
    assert len(read(api, "header-link", {**page, "auth": {"type": "header", "name": "X-Api-Key"}})[0]) == 5
    # A key sent as a query parameter is sent with every page, the next-page links included.
    api.asked.clear()
    assert len(read(api, "query-link", {**page, "auth": {"type": "query", "name": "api_key"}})[0]) == 5
    assert len(api.asked) == 3 and all(f"api_key={api.token}" in a for a in api.asked)
    assert len(read(api, "basic-link", {**page, "auth": {"type": "basic"}},
                    secrets={"username": "loader", "password": api.password})[0]) == 5
    assert len(read(api, "none-link", {**page, "auth": {"type": "none"}}, secrets={"token": ""})[0]) == 5
    for style, spec, secrets, why in (
            ("bearer-link", page, {"token": "wrong"}, "refused the credentials"),
            ("bearer-link", page, {"token": ""}, "token is missing"),
            ("basic-link", {**page, "auth": {"type": "basic"}}, {}, "username and password")):
        with pytest.raises(SaasError, match=why) as e:
            read(api, style, spec, secrets=secrets)
        assert api.token not in str(e.value) and api.password not in str(e.value)


def test_what_stops_a_run(api):
    page = {"records": "results", "pagination": {"type": "link"}}
    api.fail = [429, 503]                                    # retried, waiting as told
    got, slept = read(api, "bearer-link", page)
    assert len(got) == 5 and len(slept) == 2
    api.fail = [500, 500, 500, 500]
    with pytest.raises(SaasError, match="answered 500 four times"):
        read(api, "bearer-link", page)
    api.fail = []
    api.redirect_to = "http://169.254.169.254/latest/meta-data/"       # a redirect is never followed
    with pytest.raises(SaasError, match="redirected"):
        read(api, "bearer-link", page)
    api.redirect_to = None
    api.evil_next = "http://evil.example.com/customers?start=2"        # the next page on another host: the key stays home
    with pytest.raises(SaasError, match="another host"):
        read(api, "bearer-link", page)
    api.evil_next = None
    with pytest.raises(SaasError, match="not JSON"):
        read(api, "bearer-notjson", {"records": ""})
    with pytest.raises(SaasError, match="no list of records"):
        read(api, "bearer-cursor", {"records": "meta"})
    with pytest.raises(SaasError, match="has no uid"):
        read(api, "bearer-none", {"records": "", "id": "uid"})
    with pytest.raises(SaasError, match="answered 404"):
        with httpx.Client() as http:
            list(rest_records(http, {"base_url": f"{api.base}/bearer-none", "token": api.token}, "nothing", ["name"],
                              check_spec({"records": ""}), None, allow, error=SaasError))
    # An API that cannot be reached is named by the kind of failure only: with a key in the query, a client's own
    # message could carry it.
    with pytest.raises(SaasError, match="could not reach the API") as e:
        with httpx.Client(timeout=2) as http:
            list(rest_records(http, {"base_url": "http://127.0.0.1:9", "token": api.token}, "customers", ["name"],
                              check_spec({"records": "", "auth": {"type": "query", "name": "api_key"}}), None, allow,
                              error=SaasError))
    assert api.token not in str(e.value) and "127.0.0.1" not in str(e.value)
    # Limits: pages per run, and a response's size.
    import os

    os.environ["CAIRN_SAAS_REST_MAX_PAGES"] = "2"
    try:
        with pytest.raises(SaasError, match="more than 2 pages"):
            read(api, "bearer-link", page)
    finally:
        del os.environ["CAIRN_SAAS_REST_MAX_PAGES"]
    os.environ["CAIRN_SAAS_REST_MAX_BYTES"] = "50"
    try:
        with pytest.raises(SaasError, match="over 50 bytes"):
            read(api, "bearer-link", page)
    finally:
        del os.environ["CAIRN_SAAS_REST_MAX_BYTES"]
    # The address check Cairn really uses: this machine and the cloud metadata address are refused.
    with pytest.raises(SaasError):
        read(api, "bearer-link", page, check=_public)
    for base in ("https://169.254.169.254", "https://user:pw@api.example.com", "https://api.example.com/?x=1", ""):
        with pytest.raises(SaasError):
            with httpx.Client() as http:
                list(rest_records(http, {"base_url": base, "token": "t"}, "customers", ["name"],
                                  check_spec({"records": ""}), None, _public, error=SaasError))


def test_a_spec_is_checked():
    assert check_spec({})["auth"] == {"type": "bearer"} and check_spec({})["pagination"] == {"type": "none"}
    assert check_spec({"records": "data.items"})["id"] == "id"
    full = check_spec({**CURSOR, "params": {"active": True, "n": 5}, "auth": {"type": "query", "name": "api_key"}})
    assert full["params"] == {"active": "true", "n": "5"} and full["pagination"]["param"] == "starting_after"
    bad = [
        None, [], {"oops": 1},
        {"records": "a b"}, {"id": ""}, {"id": "a..b"},
        {"modified": "updated_at"}, {"cursor_param": "since"},                    # they go together
        {"params": {"a b": "x"}}, {"params": {"k": {"nested": 1}}}, {"params": {f"p{i}": "x" for i in range(21)}},
        {"auth": {"type": "oauth"}}, {"auth": {"type": "header"}}, {"auth": {"type": "header", "name": "Host"}},
        {"auth": {"type": "header", "name": "X Key"}}, {"auth": {"type": "bearer", "name": "x"}},
        {"auth": {"type": "query", "name": "a&b"}},
        {"pagination": {"type": "scroll"}}, {"pagination": {"type": "cursor", "param": "after"}},
        {"pagination": {"type": "offset", "limit": 0}}, {"pagination": {"type": "page", "start": 5}},
        {"pagination": {"type": "none", "param": "x"}}, {"pagination": {"type": "link", "path": "a b"}},
        {"params": {"offset": "1"}, "pagination": {"type": "offset"}},            # one parameter, two uses
        {**CURSOR, "pagination": {"type": "cursor", "param": "updated_since", "path": "next"}},
    ]
    for spec in bad:
        with pytest.raises(RestSpecError):
            check_spec(spec)
