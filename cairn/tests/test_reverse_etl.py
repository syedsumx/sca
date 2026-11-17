"""Reverse ETL (cairn.pipelines.reverse_etl) without Trino: the source rows are given, the apps answer through
httpx's mock transport. The end-to-end test on real Trino is tests/test_reverse_etl_e2e.py."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.coreproxy.masking import DatasetPlan
from cairn.pipelines.saas import SaasError

OWNER = "urn:li:corpuser:ana"


class World:
    def __init__(self, engine, monkeypatch):
        from cairn.pipelines import reverse_etl
        from cairn.pipelines.reverse_etl import ReverseSyncs

        self.rows: list[list] = []                       # the source, as the owner reads it: (email, score)
        self.secrets = {"keys": {"access_token": "tok-a", "base_url": "https://app-a.example",
                                 "instance_url": "https://sf-a.example"},
                        "other": {"access_token": "tok-b", "base_url": "https://app-b.example",
                                  "instance_url": "https://sf-b.example"}}
        self.handler = self.default
        self.calls: list[tuple[str, dict]] = []          # (host, body)
        self.app: dict[str, dict[str, dict]] = {}        # host -> key -> properties

        def read_secret(owner, scope, key):
            if key not in self.secrets.get(scope, {}):
                raise LookupError("no such secret")      # what secret scopes say of an absent key
            return self.secrets[scope][key]

        real = httpx.Client
        monkeypatch.setattr(reverse_etl.httpx, "Client",
                            lambda **kw: real(transport=httpx.MockTransport(lambda r: self.handler(r)), **kw))
        monkeypatch.setattr("time.sleep", lambda s: self.slept.append(s))
        self.slept: list[float] = []
        self.rs = ReverseSyncs(engine, lambda owner, sql, limit: (["email", "score"], list(self.rows), False),
                               lambda owner, urn: DatasetPlan(), lambda owner, urn: True, read_secret,
                               lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")

    def default(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.host, body))
        if request.url.path.endswith("/batch/upsert"):
            for i in body["inputs"]:
                self.app.setdefault(request.url.host, {})[i["id"]] = i["properties"]
            return httpx.Response(200, json={"status": "COMPLETE", "results": []})
        if "/composite/sobjects/" in request.url.path:
            key = request.url.path.rsplit("/", 1)[1]
            for r in body["records"]:
                self.app.setdefault(request.url.host, {})[r[key]] = r
            return httpx.Response(200, json=[{"success": True, "errors": []} for _ in body["records"]])
        return httpx.Response(404, json={})

    def hubspot(self, name="seg", scope="keys", **kw) -> dict:
        return {"name": name, "source": "sales.segment", "kind": "hubspot", "object": "contacts",
                "key": {"column": "email", "field": "email"}, "mapping": {"score": "lead_score"},
                "secret_scope": scope, **kw}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(engine)
    return World(engine, monkeypatch)


def test_a_sync_sends_new_and_changed_rows(world):
    world.rows = [["kim@acme.com", 90], ["lee@acme.com", 70]]
    world.rs.save(OWNER, world.hubspot())
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 2
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 0
    assert world.app["app-a.example"]["lee@acme.com"] == {"lead_score": "70"}


@pytest.mark.parametrize("kind", ["google_sheets", "rest", "delta_sharing"])
def test_only_hubspot_and_salesforce_are_destinations(world, kind):
    # The SaaS connectors' kinds grew sources that cannot be written to; a sync of one of them was saved and then
    # ran against Salesforce's endpoints with that kind's keys.
    with pytest.raises(SaasError, match="kind"):
        world.rs.save(OWNER, world.hubspot(kind=kind))
