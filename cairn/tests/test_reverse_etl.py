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
        monkeypatch.setattr(reverse_etl, "_wait", lambda s: self.slept.append(s), raising=False)
        self.slept: list[float] = []
        self.rs = ReverseSyncs(engine, lambda owner, sql, limit: (["email", "score"], list(self.rows), False),
                               lambda owner, urn: DatasetPlan(), lambda owner, urn: True, read_secret,
                               lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)",
                               audit=self, notify=lambda *a: self.told.append(a))
        self.audited: list[tuple] = []
        self.told: list[tuple] = []

    def record(self, *a) -> None:                        # the audit log
        self.audited.append(a)

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


def test_another_account_gets_every_row(world):
    # The scope's keys say which account the rows go to: a sync moved to another scope (another HubSpot portal)
    # kept the first account's "already sent" record, and the new account got only rows that later changed.
    world.rows = [["kim@acme.com", 90], ["lee@acme.com", 70]]
    world.rs.save(OWNER, world.hubspot())
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 2
    world.rs.save(OWNER, world.hubspot(scope="other"), "seg")
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 2
    assert set(world.app["app-b.example"]) == {"kim@acme.com", "lee@acme.com"}
    # A change that does not move the rows elsewhere (here the schedule) keeps the record: nothing is sent again.
    world.rs.save(OWNER, world.hubspot(scope="other", cron="0 * * * *"), "seg")
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 0


def test_a_rejection_the_app_does_not_tie_to_records_fails_them_all(world):
    # HubSpot's 207 names the rejected ids in each error's context; an error without them was read as "every record
    # accepted", so those rows were remembered as sent and never tried again.
    world.rows = [["kim@acme.com", 90], ["lee@acme.com", 70]]
    world.rs.save(OWNER, world.hubspot())
    world.handler = lambda r: httpx.Response(207, json={"status": "COMPLETE", "results": [], "errors": [
        {"category": "VALIDATION_ERROR", "message": "Property values were not valid"}]})
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert (r["status"], r["sent"], r["failed"]) == ("partial", 0, 2)
    world.handler = world.default
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 2


def test_a_salesforce_answer_that_does_not_cover_each_record_is_refused(world):
    # Composite upsert answers one result per record, in order; a shorter answer shifted every later result onto
    # the wrong rows (one batch's failures counted against the next one's records).
    world.rows = [[f"p{i}@acme.com", i] for i in range(3)]
    world.rs.save(OWNER, {**world.hubspot(), "kind": "salesforce", "object": "Contact",
                          "key": {"column": "email", "field": "Email"}})
    world.handler = lambda r: httpx.Response(200, json=[{"success": True, "errors": []}])
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert r["status"] == "failed" and r["sent"] == 0 and "results" in r["error"]
    world.handler = world.default
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 3


def test_a_run_that_stops_midway_keeps_what_the_app_accepted(world):
    # Records went out in calls of 100 but were remembered only once every call had answered: a call refused after
    # others were accepted forgot them all, so a large sync that always stopped at the same call sent everything
    # again on each run and never got further.
    world.rows = [[f"p{i}@acme.com", i] for i in range(250)]
    world.rs.save(OWNER, world.hubspot())

    def third_refused(request):
        if len(world.calls) == 2:
            world.calls.append(("refused", {}))
            return httpx.Response(500, json={"message": "internal error"})
        return world.default(request)
    world.handler = third_refused
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert r["status"] == "failed" and r["sent"] == 200 and "500" in r["error"]
    world.handler, world.calls = world.default, []
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 50
    assert [len(b["inputs"]) for _, b in world.calls] == [50]


def test_the_apps_rate_limits_are_waited_out(world):
    # HubSpot (429 with Retry-After) and Salesforce limit how fast a sync may call; a 429 failed the run at once.
    world.rows = [["kim@acme.com", 90]]
    world.rs.save(OWNER, world.hubspot())
    limited = [2]

    def busy(request):
        if limited[0]:
            limited[0] -= 1
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"category": "RATE_LIMITS"})
        return world.default(request)
    world.handler = busy
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert (r["status"], r["sent"]) == ("ok", 1) and world.slept == [3.0, 3.0]
    # An app that stays busy fails the run after a bounded number of tries, never a wait without end.
    world.rows = [["kim@acme.com", 91]]
    world.handler = lambda request: httpx.Response(429, headers={"Retry-After": "86400"}, json={})
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert r["status"] == "failed" and "429" in r["error"]
    assert len(world.slept) <= 2 + 5 and max(world.slept) <= 60


def test_a_key_on_several_rows_is_not_sent(world):
    # Two source rows with one key both went out: HubSpot refuses a batch with an id twice (the whole call), and
    # where an app took both, the record ended with whichever came last, and was sent again on every run because
    # the remembered hash was the other row's.
    world.rows = [["kim@acme.com", 90], ["lee@acme.com", 70], ["kim@acme.com", 10]]
    world.rs.save(OWNER, world.hubspot())
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert (r["status"], r["sent"], r["failed"]) == ("partial", 1, 1) and "2 rows" in r["error"]
    assert set(world.app["app-a.example"]) == {"lee@acme.com"}
    world.calls = []
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert (r["sent"], r["failed"], world.calls) == (0, 1, [])
    world.rows = world.rows[:2]                        # the duplicate is gone: the key goes out
    assert world.rs.run_now(OWNER, "seg", wait=True)["sent"] == 1
    assert world.app["app-a.example"]["kim@acme.com"] == {"lead_score": "90"}


@pytest.mark.parametrize("mapping", [{"score": "email"}, {"score": "Email"}, {"score": "lead_score", "region": "lead_score"}])
def test_one_app_field_is_written_from_one_column(world, mapping):
    # A mapped field equal to the key field put another column's value where the key goes (Salesforce: over the
    # external id in the record; HubSpot: the matched record's email changed to it), and two columns mapped to one
    # field sent whichever came last.
    with pytest.raises(SaasError, match="mapping"):
        world.rs.save(OWNER, world.hubspot(mapping=mapping))
    assert world.rs.save(OWNER, world.hubspot(mapping={"email": "email", "score": "lead_score"}))["name"] == "seg"


@pytest.mark.parametrize("kind", ["hubspot", "salesforce"])
def test_an_apps_refusal_never_puts_row_values_in_the_audit_log_or_logs(world, kind, caplog):
    # A refused call's answer was copied into the run's error, and from there into the audit log and the server's
    # log; the apps' validation errors quote the values they refused (here a row's email).
    world.rows = [["kim@acme", 90]]
    sync = world.hubspot()
    if kind == "salesforce":
        sync = {**sync, "kind": "salesforce", "object": "Contact", "key": {"column": "email", "field": "Email"}}
    world.rs.save(OWNER, sync)
    answer = ({"status": "error", "category": "VALIDATION_ERROR", "message": "Email kim@acme is invalid"}
              if kind == "hubspot" else [{"errorCode": "INVALID_EMAIL_ADDRESS", "message": "Email: kim@acme"}])
    world.handler = lambda r: httpx.Response(400, json=answer)
    r = world.rs.run_now(OWNER, "seg", wait=True)
    assert r["status"] == "failed" and "400" in r["error"]
    assert ("VALIDATION_ERROR" if kind == "hubspot" else "INVALID_EMAIL_ADDRESS") in r["error"]
    assert "kim@acme" not in json.dumps(world.audited, default=str) and "kim@acme" not in caplog.text
